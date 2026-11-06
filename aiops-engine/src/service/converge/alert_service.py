"""alert：最近的異常 → 合成一件（investigator/alerts.py）→ 新的那件跑一次根因分析 → 寫 aiops-alerts。

Grafana 讀 aiops-alerts 發通知（grafana/helmchart/files/）：status open 的每一件一則，依 dept 分送；
@timestamp 是「這次執行還看到它開著」的時間往前 1 分鐘（心跳；為什麼往前見 STAMP_LAG），所以 Grafana 只要查最近幾分鐘有 @timestamp 的 open 件。
這支要定期跑（建議每 1～2 分鐘，跟 rules、detect 之後）。
"""
from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import service.rca.llm as LLM
from core.connection import ES
from core.entity.alert import Alert, stamp_of
from core.entity.incident import Incident
from core.repository import Repos
from scenario import Chain, load_scenario
from scenario import load_scenario

import service.converge.merge as AL
import service.converge.severity as SEV
import service.rca.similar as SIM


def _dur(s: str) -> timedelta:
    """30s、30m、2h、1d"""
    s = str(s)
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[s[-1]]: int(s[:-1])})


def grade_alert(repo: Repos, cfg: dict, al: Alert, now: datetime) -> None:
    """這件的嚴重度：訊號廣度＋持續時間＋換版上限（investigator/severity.py）。要全部異常（不只這一輪的）才算得出持續。
    訊號怎麼分類（IT 黃金訊號／工廠參數家族）、哪些 level 的 critical 直接算 critical、「換版」是什麼（版號／recipe 變更）由情境給預設，
    cfg 的 alerts.severity.classes、alerts.critical_levels 可覆蓋。"""
    sc = repo.scenario
    acfg = cfg.get("alerts", {})
    sev_cfg = dict(acfg.get("severity") or {})
    classes = SEV.parse_classes(sev_cfg.pop("classes", None) or sc.severity_classes)
    opts = SEV.GradeOptions(**sev_cfg)
    critical_levels = set(acfg.get("critical_levels") or sc.critical_levels)
    have = {a["_id"]: a for a in al.anoms}
    missing = [i for i in al.anomaly_ids if i not in have]
    if missing:
        try:
            got = repo.anomalies.get_many(missing, ["@timestamp", "series", "keys", "severity", "level"])
            have.update({i: {**d, "_id": i} for i, d in got.items()})
        except Exception as e:  # noqa: BLE001  讀不到就只用這一輪的：寧可低估持續，不擋告警
            print(f"  讀不到 {al.alert_id} 的舊異常（{type(e).__name__}），持續時間只看這一輪")
    anoms = list(have.values())
    own = [a for a in anoms if AL.entity_of(a, sc)[1] == al.service] or anoms
    dep = None
    if acfg.get("deploy_grace", True):
        try:
            since = al.started_at - timedelta(minutes=opts.deploy_grace)
            dep = sc.change(repo, cfg, al.service, since, now)
        except Exception as e:  # noqa: BLE001
            print(f"  查不到 {al.service} 的換版時間（{type(e).__name__}）")
    golden = [a for a in own if SEV.signal_class(a.get("series"), classes) not in ("traffic", "other")]
    g = SEV.grade([a.get("series") for a in own], [AL._ts(a["@timestamp"]) for a in own],
                  members=len({(a.get("series"), sc.route_of(a.get("keys") or {})) for a in golden}),
                  rule_critical=any(a.get("severity") == "critical" and a.get("level") in critical_levels for a in own),
                  now=now, deploy_at=dep, opts=opts, classes=classes)
    if g["severity"] != al.severity:
        al.upgraded = AL.SEVERITY.get(g["severity"], 1) > AL.SEVERITY.get(al.severity or "warning", 1) and not al.new
    al.severity, al.severity_why = g["severity"], g["why"]
    if dep is not None:
        al.deploy_at = dep


def with_feedback_footer(report: str | None, feedback_url: str | None) -> str | None:
    """報告全文最後加一行老師傅回饋的網址：Grafana 表格點眼睛（Inspect value）看全文時，看完最下面就是回饋入口。
    報告會跟著件一輪一輪寫回去，已經有這行就不再加。"""
    if not report or not feedback_url:
        return report
    line = f"資深維運專家（老師傅）回饋：{feedback_url}"
    return report if line in report else f"{report.rstrip()}\n\n---\n{line}\n"


def titled(title: str | None, report: str | None, llm: str | None = None) -> str:
    """Grafana 表格的 title 格用：第一行標題（表格只顯示這行），點眼睛看整份——
    【AI 摘要】、【根因分析】（報告全文，最下面是老師傅回饋的網址）。
    報告最前面本來就引用了 AI 摘要（「> …」），已經放在【AI 摘要】就從全文拿掉，不重複。"""
    parts = [title or ""]
    body = report or ""
    if llm:
        quoted = "\n".join(f"> {l}" for l in llm.splitlines())
        if body.startswith(quoted):
            body = body[len(quoted):].lstrip("\n")
        parts.append(f"【AI 摘要（LLM，只是參考）】\n{llm}")
    if body:
        parts.append(f"【根因分析】\n{body}")
    return "\n\n".join(parts)


def llm_summary(al: Alert, findings: dict | None, similar: list[dict], acfg: dict, unit: str = "請求") -> dict | None:
    """LLM 摘要：只加註、不擋告警。輸入包裡有 NaN、模型掛了、程式有 bug，都只是這件沒有摘要，這一輪照寫。"""
    if not acfg.get("llm", True):
        return None
    try:
        pack = LLM.build_pack({**al.to_doc(), "minutes": int((al.updated_at - al.started_at).total_seconds() // 60)},
                              findings, similar, unit=unit)
        llm = LLM.summarize(pack, acfg.get("llm_provider"))
    except Exception as e:
        print(f"  LLM 摘要失敗（{type(e).__name__}: {str(e)[:80]}），這件沒有摘要")
        return None
    if not llm["ok"]:
        print(f"  LLM 摘要不顯示（{llm['reason']}）")
    return llm


def has_trace(findings: dict | None) -> bool:
    """根因分析有沒有看到這個服務的 trace：findings.requests.requests > 0。沒有 findings（分析失敗）算沒有。"""
    return bool(((findings or {}).get("requests") or {}).get("requests"))


def attempts(al: Alert) -> int:
    """這件的根因分析跑過幾次。舊的件沒有這個欄位：已經有報告就當作跑過 1 次。"""
    return int(al.rca_attempts or (1 if al.report else 0))


def needs_rerun(al: Alert, now: datetime, acfg: dict) -> bool:
    """開件太快、trace 還沒合成完（合成晚 1～2 分鐘）時第一份報告是空的：沒有 trace 的件隔 rca_retry_after 重跑一次，
    最多 rca_retry_max 次（服務本來就沒埋點的，不會永遠重跑）。報告已經有 trace、或重跑次數用完就不再跑。"""
    if not acfg.get("rca_retry", True) or al.new or not al.report:
        return False
    if has_trace(al.findings) or attempts(al) >= int(acfg.get("rca_retry_max", 5)):
        return False
    return al.report_at is None or now - al.report_at >= _dur(acfg.get("rca_retry_after", "2m"))


def wants_rerun(al: Alert, now: datetime, acfg: dict) -> tuple[bool, bool]:
    """這一輪要不要重跑根因分析：回 (要不要, 是不是人要求的)。
    人要求的＝api POST /api/rca 標在件上的 rca_requested（Grafana 的「重跑根因分析」按鈕）：不管有沒有 trace 都重跑；標記在這裡拿掉，不寫回去。
    自動的＝needs_rerun（沒 trace 的件隔一陣子再試）。只有開著的 service 類的件會重跑；rca 關了都不跑。"""
    forced = bool(al.rca_requested)
    al.rca_requested, al.rca_requested_at = None, None      # 看過就拿掉（to_doc 不寫 None，ES 那筆的標記就消失）
    wants = bool(acfg.get("rca", True)) and al.category == "service" and al.status == "open"
    return wants and (needs_rerun(al, now, acfg) or forced), wants and forced


def _chain(repo: Repos, cfg: dict, acfg: dict, anoms: list[dict], open_alerts: list[Alert], start: datetime, now: datetime) -> Chain:
    """跨對象（一層）要不要併、依什麼併，由情境決定（IT：trace 的呼叫關係；工廠：履歷的前後站；只有指標：不併）。
    查失敗只是這一輪不併跨對象，不擋告警。"""
    if not acfg.get("chain", True):
        return Chain()
    try:
        return repo.scenario.chain(repo, cfg, anoms, open_alerts, start, now)
    except Exception as e:  # noqa: BLE001
        print(f"  跨對象的關聯查不到（{type(e).__name__}: {str(e)[:80]}），這一輪不併跨對象")
        return Chain()


def enrich(repo: Repos, cfg: dict, al: Alert, now: datetime) -> None:
    """這一件補歸屬（dept、group）與嚴重度。"""
    al.dept, al.group = AL.owner(al, repo.scenario) if al.anoms else (al.dept, al.group or "unassigned")
    if al.category == "service" and not al.merged_into:
        grade_alert(repo, cfg, al, now)


def _rca(repo: Repos, cfg: dict, acfg: dict, al: Alert, interval: timedelta, investigate) -> tuple[str, dict | None]:
    """跑一次根因分析：時間是這件的開始到最後一筆異常再往後一個 interval；只有一支 API 時只查那支
    （連帶進來的上游的 API 不算：根因分析以這件的服務＝下游跑）。
    rca_lead：根因分析的窗往前多看（工廠：ML 在 run 結束才報，出事的 run 是 started_at 之前就開始的；IT 是 0）。失敗不擋這一件。"""
    try:
        return investigate(cfg, Namespace(service=al.service, route=AL.rca_route(al, repo.scenario),
                                          start=(al.started_at - _dur(acfg.get("rca_lead", "0s"))).isoformat(),
                                          end=(al.updated_at + interval).isoformat(), at=None, window=None))
    except Exception as e:   # noqa: BLE001
        return f"根因分析失敗：{e}", None


def _similar(repo: Repos, acfg: dict, al: Alert, findings: dict | None) -> list[dict]:
    """相似事故（老師傅確認過的）；查失敗當沒有。"""
    if not acfg.get("similar", True):
        return []
    try:
        return SIM.search(repo, SIM.fingerprint(al, findings), weights=acfg.get("similar_weights"))
    except Exception as e:  # noqa: BLE001
        print(f"  相似事故檢索失敗：{e}")
        return []


def _with_head(report: str | None, llm: dict | None, similar: list[dict], cls: dict | None) -> str | None:
    """報告最前面：LLM 摘要（引言）＋相似事故的分類。"""
    head = ([f"> {l}" for l in llm["text"].splitlines()] + [""] if llm and llm["ok"] else []) + SIM.format_similar(similar, cls=cls)
    return "\n".join(head) + "\n" + report if head and report else report


def investigate_alert(repo: Repos, cfg: dict, acfg: dict, al: Alert, now: datetime, interval: timedelta, investigate) -> None:
    """新開的件、沒報告的件、要重跑的件：根因分析 → 相似事故分類 → LLM 摘要（只加註）。
    結果放 al.report_text、al.llm（這一輪用，不寫 ES）、al.findings、al.similar、al.suggested_category（寫進 ES）。"""
    report, llm = al.report, None
    rerun, forced = wants_rerun(al, now, acfg)   # forced：人按了「重跑根因分析」（api POST /api/rca）
    fresh = (al.new or not report) and acfg.get("rca", True) and al.category == "service" and al.status == "open"
    if not (fresh or rerun):
        al.report_text, al.llm = report, llm
        return
    report, findings = _rca(repo, cfg, acfg, al, interval, investigate)
    al.report_at = now
    al.rca_attempts = attempts(al) + 1
    if rerun and not forced and not has_trace(findings):
        # 重跑了還是沒有 trace：不覆蓋舊的報告、不再叫模型，只記下又試了一次（下一輪再看）
        print(f"  ⏳ {al.alert_id}：trace 還沒就緒（第 {al.rca_attempts} 次），先不換報告")
        al.report_text, al.llm = al.report, None
        return
    if rerun:
        al.rerun = True
        al.llm_summary = None      # 舊的摘要是「沒有 trace」時寫的（可能說 trace 沒變慢），換掉
        print(f"  🔁 {al.alert_id}：{'人要求' if forced else 'trace 到了，'}重跑根因分析（第 {al.rca_attempts} 次）")
    similar = _similar(repo, acfg, al, findings)
    # case 只做分類：像哪一類＋信心，寫進告警；報告、摘要不列 case 的處置
    cls = SIM.classify(similar)
    al.suggested_category = cls["category"] if cls else None
    al.category_confidence = cls["confidence"] if cls else None
    llm = llm_summary(al, findings, similar, acfg, repo.scenario.unit_text())
    al.findings, al.similar = findings, similar
    al.report_text, al.llm = _with_head(report, llm, similar, cls), llm


def alert_doc(al: Alert, acfg: dict, now: datetime, tz: ZoneInfo, relinked: set[str], sc) -> dict:
    """要寫進 aiops-alerts 的那份：有變的件重寫標題與摘要；報告全文加回饋連結、連結、筆數、LLM 摘要、@timestamp 補上；to_doc 不寫執行期欄位、時間轉字串。"""
    if al.anoms or al.new or al.rerun or not al.summary or al.alert_id in relinked:
        al.title, al.summary = AL.headline(al, sc=sc), AL.summary(al, al.report_text, tz, al.similar, sc=sc)
    url, furl = acfg.get("report_url"), acfg.get("feedback_url")
    fb = furl.format(alert_id=al.alert_id) if furl else None
    al.report = with_feedback_footer(al.report_text, fb)
    al.report_url = url.format(alert_id=al.alert_id) if url else None
    al.feedback_url = fb
    al.anomaly_count = len(al.anomaly_ids)
    al.llm_summary = (al.llm or {}).get("text") or al.llm_summary
    al.llm_provider = (al.llm or {}).get("provider") or al.llm_provider
    al.timestamp = al.resolved_at if al.status == "resolved" else stamp_of(now)
    al.report_titled = titled(al.title, al.report, al.llm_summary)
    return al.to_doc()


def heartbeat_doc(now: datetime) -> tuple[str, dict]:
    """每次跑都寫一筆（不是一件事，status ok；Grafana 查 status:open 不會抓到）。
    aiops-alerts 10 分鐘沒有任何寫入 → Grafana 的「AIOps 停了」規則送平台（configs/grafana/alert-rules.yaml）。"""
    return ("heartbeat", {"@timestamp": stamp_of(now).isoformat(), "alert_id": "heartbeat", "category": "heartbeat", "status": "ok",
                          "service": "aiops", "title": "investigator alert 心跳", "group": "unassigned", "started_at": now.isoformat(),
                          "updated_at": now.isoformat(), "anomaly_ids": [], "series": [], "routes": [], "anomaly_count": 0})


def announce(al: Alert, doc: dict) -> str | None:
    """印這一件這一輪怎麼了；回 "new"／"resolved" 給統計，其他 None。"""
    if al.new and al.status == "resolved":   # 已經過去的（例如這支停了一陣子才跑）：記下來，不算新開、不通知
        print(f"  ⏺ 已經過去的：{doc['title']}")
    elif al.new:
        print(f"  🆕 {doc['title']}（{doc.get('dept') or '—'}／{doc.get('group')}）")
        return "new"
    elif al.status == "resolved":
        print(f"  ✅ 結束：{doc['title']}")
        return "resolved"
    elif al.upgraded:
        print(f"  ⬆️ {doc['title']}")
    return None


def run(cfg: dict, args, investigate) -> int:
    """一輪：讀開著的件與最近的異常 → 併成件（alerts.merge）→ 每件補歸屬、嚴重度、根因分析 → 寫回 aiops-alerts、異常標 alert_id、開事故。
    investigate：根因分析的函式（investigator.__main__.investigate_full），傳進來避免循環 import。"""
    repo = Repos(ES(), load_scenario())
    repo.alerts.ensure()
    acfg = cfg.get("alerts", {})
    tz = ZoneInfo(cfg.get("timezone", "Asia/Taipei"))
    now = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else datetime.now(timezone.utc)
    close_after, lookback = _dur(acfg.get("close_after", "30m")), _dur(acfg.get("lookback", "2h"))
    interval = _dur(cfg.get("interval", "5m"))
    open_alerts = repo.alerts.open()
    oldest = min([a.updated_at for a in open_alerts], default=now)
    start = min(now - lookback, oldest - close_after)
    anoms = repo.anomalies.between(start, now)
    chain = _chain(repo, cfg, acfg, anoms, open_alerts, start, now)
    alerts = AL.merge(open_alerts, anoms, now, close_after, repo.alerts.resolved_anomaly_ids(start - close_after), chain.calls, chain.links,
                      scenario=repo.scenario)
    for al in alerts:
        enrich(repo, cfg, al, now)
        investigate_alert(repo, cfg, acfg, al, now, interval, investigate)
    # 開著的件之間：卡同一個外部依賴（同一個 Oracle）的互相標同源（不合併）；有變的件摘要重寫
    relinked = AL.link_shared(alerts)
    docs, incidents, tags, new, resolved = [], [], [], 0, 0
    for al in alerts:
        doc = alert_doc(al, acfg, now, tz, relinked, repo.scenario)
        docs.append((al.alert_id, doc))
        if al.merged_into:
            # 上游那件整件併進下游：它的異常改標下游那件（下游那件這輪有 _anoms，會一起寫）；這筆標 resolved 寫回去，
            # 它開的事故改成 merged（還是 auto 才會被改；老師傅確認過的不動）
            print(f"  🔗 併進 {al.merged_into}：{doc['title']}")
            if al.category == "service":
                incidents.append((al.alert_id, Incident.from_alert(al, doc, al.findings, status="merged", merged_into=al.merged_into).to_doc()))
            continue
        if al.anoms or al.status == "resolved":
            # 這輪有異常併進來、或這輪結束的件：把 alert_id 寫回它的每筆異常（連同之前併的：改版前就開著的件，結束時補得到）
            tags += [(i, al.alert_id) for i in dict.fromkeys([a["_id"] for a in al.anoms] + al.anomaly_ids)]
        if al.category == "service" and (al.new or al.anoms or al.rerun or al.status == "resolved"):
            incidents.append((al.alert_id, Incident.from_alert(al, doc, al.findings).to_doc()))
        outcome = announce(al, doc)
        new += outcome == "new"
        resolved += outcome == "resolved"
    docs.append(heartbeat_doc(now))
    repo.anomalies.tag_alert(tags)   # 先寫回異常、再寫件：件寫失敗下一輪會重做；反過來這些異常就永遠沒標到
    repo.alerts.bulk(docs)
    repo.incidents.upsert_auto(incidents)    # 已被老師傅確認的不覆蓋（原子，不是先讀再寫）
    still = sum(1 for _, d in docs if d["status"] == "open")
    print(f"{now.astimezone(tz):%m-%d %H:%M}：新開 {new} 件、結束 {resolved} 件、進行中 {still} 件")
    return 0

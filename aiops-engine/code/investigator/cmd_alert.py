"""alert：最近的異常 → 合成一件（investigator/alerts.py）→ 新的那件跑一次根因分析 → 寫 aiops-alerts。

Grafana 讀 aiops-alerts 發通知（grafana/code/）：status open 的每一件一則，依 dept 分送；
@timestamp 是「這次執行還看到它開著」的時間往前 1 分鐘（心跳；為什麼往前見 STAMP_LAG），所以 Grafana 只要查最近幾分鐘有 @timestamp 的 open 件。
這支要定期跑（建議每 1～2 分鐘，跟 rules、detect 之後）。
"""
from __future__ import annotations

import json

from argparse import Namespace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core import llm as LLM
from core.es import ES

from . import alerts as AL
from . import similar as SIM

INCIDENT_INDEX = "aiops-incidents"

ALERT_INDEX = "aiops-alerts"
# 開著的件、心跳的 @timestamp 往前寫這麼多（docs/pdca/phase3-locate/2026-10-alert-flap.md）。
# 一件只有一筆、每輪覆蓋；Grafana 的查詢範圍是「排定的評估時間往前 N 分鐘」，查詢卻晚幾秒才送出。
# 覆蓋剛好落在這幾秒之間時，@timestamp 比範圍的結束點還晚，Grafana 查到 0 筆、當成恢復（實測）。
# 往前寫 1 分鐘：主機間時鐘差、Grafana 的延遲都遠小於它；規則的窗是 5 分鐘（心跳 10 分鐘），還剩 4 分鐘的餘裕。
STAMP_LAG = timedelta(minutes=1)


def stamp_of(now: datetime) -> datetime:
    """開著的件、心跳寫進 aiops-alerts 的 @timestamp（Grafana 用它判斷「最近還有沒有」）。"""
    return now - STAMP_LAG


def _dur(s: str) -> timedelta:
    """30s、30m、2h、1d"""
    s = str(s)
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[s[-1]]: int(s[:-1])})
ALERT_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "alert_id": {"type": "keyword"}, "category": {"type": "keyword"},
    "service": {"type": "keyword"}, "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "severity": {"type": "keyword"}, "status": {"type": "keyword"},
    "started_at": {"type": "date"}, "updated_at": {"type": "date"}, "resolved_at": {"type": "date"},
    "anomaly_ids": {"type": "keyword"}, "anomaly_count": {"type": "integer"}, "series": {"type": "keyword"},
    "routes": {"type": "keyword"},
    # Grafana 的告警只能帶 labels（terms 分組的值），所以標題、摘要、報告連結都存成 keyword
    "title": {"type": "keyword", "ignore_above": 1024}, "summary": {"type": "keyword", "ignore_above": 4096},
    "report_url": {"type": "keyword"}, "report": {"type": "text", "index": False}, "report_at": {"type": "date"},
    "feedback_url": {"type": "keyword"},
    # 結構化的根因分析結果（相似事故檢索、LLM 用；不索引）、相似事故、LLM 摘要
    "findings": {"type": "object", "enabled": False}, "similar": {"type": "object", "enabled": False},
    "llm_summary": {"type": "text", "index": False}, "llm_provider": {"type": "keyword"},
    # detector detect 每輪寫的 detect-status（detector/cmd_baseline.py）：這輪沒有 baseline 的序列數；Grafana 的 aiops-baseline-missing 查它
    "missing_baseline": {"type": "integer"},
}}}


def _anomalies(es: ES, index: str, start: datetime, end: datetime) -> list[dict]:
    out, after = [], None
    while True:
        body = {"size": 5000, "query": {"range": {"@timestamp": {"gte": start.isoformat(), "lt": end.isoformat()}}},
                "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}]}
        if after:
            body["search_after"] = after
        hits = es.search(index, body)["hits"]["hits"]
        out += [{**h["_source"], "_id": h["_id"]} for h in hits]
        if len(hits) < 5000:
            return out
        after = hits[-1]["sort"]


def _open_alerts(es: ES) -> list[dict]:
    hits = es.search(ALERT_INDEX, {"size": 10000, "query": {"term": {"status": "open"}}})["hits"]["hits"]
    return [h["_source"] for h in hits]


def _done_ids(es: ES, since: datetime) -> set:
    """最近結束的件裡的異常 id：重跑時這些異常不再開新的一件。"""
    body = {"size": 10000, "_source": ["anomaly_ids"], "query": {"bool": {"filter": [
        {"term": {"status": "resolved"}}, {"range": {"resolved_at": {"gte": since.isoformat()}}}]}}}
    return {i for h in es.search(ALERT_INDEX, body)["hits"]["hits"] for i in h["_source"].get("anomaly_ids") or []}


def _incident_doc(al: dict, doc: dict, findings: dict | None) -> dict:
    """事件整合自動開的事故（status auto）：回饋表單確認後變 confirmed，才會被相似事故檢索拿來用。"""
    return {"@timestamp": doc["@timestamp"], "alert_id": al["alert_id"], "title": doc.get("title"), "start": doc["started_at"],
            "end": doc.get("resolved_at") or doc["updated_at"], "services": [al["service"]], "severity": al["severity"],
            "status": "auto", "dept": doc.get("dept"), "group": doc.get("group"), "anomaly_ids": al["anomaly_ids"][:200],
            "detected_at": doc["started_at"], "fingerprint": SIM.fingerprint(al, findings)}


def llm_summary(al: dict, findings: dict | None, similar: list[dict], acfg: dict) -> dict | None:
    """LLM 摘要：只加註、不擋告警。輸入包裡有 NaN、模型掛了、程式有 bug，都只是這件沒有摘要，這一輪照寫。"""
    if not acfg.get("llm", True):
        return None
    try:
        pack = LLM.build_pack({**al, "minutes": int((AL._ts(al["updated_at"]) - AL._ts(al["started_at"])).total_seconds() // 60)},
                              findings, similar)
        llm = LLM.summarize(pack, acfg.get("llm_provider"))
    except Exception as e:
        print(f"  LLM 摘要失敗（{type(e).__name__}: {str(e)[:80]}），這件沒有摘要")
        return None
    if not llm["ok"]:
        print(f"  LLM 摘要不顯示（{llm['reason']}）")
    return llm


def write_incidents(es: ES, incidents: list[tuple[str, dict]]) -> int:
    """事故：只更新還是 auto 的；已被老師傅確認的（confirmed、corrected、false_positive…）一個欄位都不動。
    用 ES 的 scripted upsert 一次做完判斷和寫入（原子），不是先讀再寫：讀和寫之間老師傅送出回饋也不會被蓋掉。"""
    if not incidents:
        return 0
    from detector.feedback import INCIDENT_MAPPING
    es.ensure_index(INCIDENT_INDEX, INCIDENT_MAPPING)
    script = {"lang": "painless", "params": {},
              "source": "if (ctx._source.status == null || ctx._source.status == 'auto') { ctx._source.putAll(params.doc) } else { ctx.op = 'none' }"}
    lines = []
    for _id, doc in incidents:
        lines.append(json.dumps({"update": {"_index": INCIDENT_INDEX, "_id": _id}}))
        lines.append(json.dumps({"script": {**script, "params": {"doc": doc}}, "upsert": doc}, default=str))
    r = es.s.post(f"{es.url}/_bulk?refresh=wait_for", data="\n".join(lines) + "\n", headers={"Content-Type": "application/x-ndjson"}, timeout=300)
    body = r.json()
    if r.status_code != 200 or body.get("errors"):
        bad = [it for it in body.get("items", []) if it["update"].get("error")][:3]
        raise RuntimeError(f"寫入 {INCIDENT_INDEX} 失敗：{r.status_code} {bad or r.text[:500]}")
    return sum(1 for it in body.get("items", []) if it["update"].get("result") in ("created", "updated"))


def tag_anomalies(es: ES, index: str, tags: list[tuple[str, str]]) -> int:
    """把 alert_id 寫回這件併進來的每筆異常（aiops-anomalies）：儀表板用 alert_id:"<這件>" 就撈得到這件的異常，
    規則觸發的（沒有 status）也撈得到，不用靠時間範圍猜。tags：(異常 _id, alert_id)。
    異常已被清掉（保留期過了）的那幾筆略過，不擋這一輪。"""
    if not tags:
        return 0
    from detector.es import ANOMALY_MAPPING
    es.ensure_index(index, ANOMALY_MAPPING)    # mapping 是 dynamic false：舊 index 要補 alert_id 欄位，不然查不到
    lines = []
    for _id, aid in tags:
        lines.append(json.dumps({"update": {"_index": index, "_id": _id}}))
        lines.append(json.dumps({"doc": {"alert_id": aid}}))
    r = es.s.post(f"{es.url}/_bulk?refresh=wait_for", data="\n".join(lines) + "\n", headers={"Content-Type": "application/x-ndjson"}, timeout=300)
    body = r.json()
    items = body.get("items", [])
    bad = [it for it in items if it["update"].get("error") and it["update"]["error"].get("type") != "document_missing_exception"]
    if r.status_code != 200 or bad:
        raise RuntimeError(f"寫入 {index} 的 alert_id 失敗：{r.status_code} {bad[:3] or r.text[:500]}")
    return sum(1 for it in items if it["update"].get("result") in ("updated", "noop"))


def run(cfg: dict, args, investigate) -> int:
    """investigate：根因分析的函式（investigator.__main__.investigate_full），傳進來避免循環 import。"""
    es = ES()
    es.ensure_index(ALERT_INDEX, ALERT_MAPPING)
    acfg = cfg.get("alerts", {})
    tz = ZoneInfo(cfg.get("timezone", "Asia/Taipei"))
    now = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else datetime.now(timezone.utc)
    close_after, lookback = _dur(acfg.get("close_after", "30m")), _dur(acfg.get("lookback", "2h"))
    interval = _dur(cfg.get("interval", "5m"))
    open_alerts = _open_alerts(es)
    oldest = min([datetime.fromisoformat(str(a["updated_at"]).replace("Z", "+00:00")) for a in open_alerts], default=now)
    start = min(now - lookback, oldest - close_after)
    alerts = AL.merge(open_alerts, _anomalies(es, cfg["indices"]["anomalies"], start, now), now, close_after,
                      _done_ids(es, start - close_after))
    docs, incidents, tags, new, resolved = [], [], [], 0, 0
    for al in alerts:
        al["dept"], al["group"] = AL.owner(al) if al.get("_anoms") else (al.get("dept"), al.get("group") or "unassigned")
        report, findings, similar, llm = al.get("report"), al.get("findings"), al.get("similar") or [], None
        if (al.get("_new") or not report) and acfg.get("rca", True) and al["category"] == "service" and al["status"] == "open":
            # 新的一件跑一次根因分析：時間是這件的開始到最後一筆異常再往後一個 interval；只有一支 API 時只查那支
            route = al["routes"][0] if len(al["routes"]) == 1 else None
            try:
                report, findings = investigate(cfg, Namespace(service=al["service"], route=route, start=AL._ts(al["started_at"]).isoformat(),
                                                              end=(AL._ts(al["updated_at"]) + interval).isoformat(), at=None, window=None))
            except Exception as e:   # 根因分析失敗不擋這一件
                report, findings = f"根因分析失敗：{e}", None
            al["report_at"] = now
            # 相似事故（老師傅確認過的）→ 附在報告最前面；LLM 摘要（只加註）
            try:
                similar = SIM.search(es, SIM.fingerprint(al, findings)) if acfg.get("similar", True) else []
            except Exception as e:
                similar = []
                print(f"  相似事故檢索失敗：{e}")
            llm = llm_summary(al, findings, similar, acfg)
            head = ([f"> {l}" for l in llm["text"].splitlines()] + [""] if llm and llm["ok"] else []) + SIM.format_similar(similar)
            if head and report:
                report = "\n".join(head) + "\n" + report
            al["findings"], al["similar"] = findings, similar
        if al.get("_anoms") or al.get("_new") or not al.get("summary"):
            al["title"], al["summary"] = AL.headline(al), AL.summary(al, report, tz, similar)
        url, furl = acfg.get("report_url"), acfg.get("feedback_url")
        doc = {k: v for k, v in al.items() if not k.startswith("_")}
        doc.update({"report": report, "report_url": url.format(alert_id=al["alert_id"]) if url else None,
                    "feedback_url": furl.format(alert_id=al["alert_id"]) if furl else None,
                    "anomaly_count": len(al["anomaly_ids"]), "findings": findings, "similar": similar,
                    "llm_summary": (llm or {}).get("text") or al.get("llm_summary"), "llm_provider": (llm or {}).get("provider") or al.get("llm_provider"),
                    "@timestamp": (al["resolved_at"] if al["status"] == "resolved" else stamp_of(now))})
        doc = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in doc.items()}
        docs.append((al["alert_id"], doc))
        if al.get("_anoms") or al["status"] == "resolved":
            # 這輪有異常併進來、或這輪結束的件：把 alert_id 寫回它的每筆異常（連同之前併的：改版前就開著的件，結束時補得到）
            tags += [(i, al["alert_id"]) for i in dict.fromkeys([a["_id"] for a in al.get("_anoms") or []] + al["anomaly_ids"])]
        if al["category"] == "service" and (al.get("_new") or al.get("_anoms") or al["status"] == "resolved"):
            incidents.append((al["alert_id"], _incident_doc(al, doc, findings)))
        if al.get("_new") and al["status"] == "resolved":   # 已經過去的（例如這支停了一陣子才跑）：記下來，不算新開、不通知
            print(f"  ⏺ 已經過去的：{doc['title']}")
        elif al.get("_new"):
            new += 1
            print(f"  🆕 {doc['title']}（{doc['dept'] or '—'}／{doc['group']}）")
        elif al["status"] == "resolved":
            resolved += 1
            print(f"  ✅ 結束：{doc['title']}")
        elif al.get("_upgraded"):
            print(f"  ⬆️ {doc['title']}")
    # 心跳：每次跑都寫一筆（不是一件事，status ok；Grafana 查 status:open 不會抓到）。
    # aiops-alerts 10 分鐘沒有任何寫入 → Grafana 的「AIOps 停了」規則送平台（configs/grafana/alert-rules.yaml）
    docs.append(("heartbeat", {"@timestamp": stamp_of(now).isoformat(), "alert_id": "heartbeat", "category": "heartbeat", "status": "ok",
                               "service": "aiops", "title": "investigator alert 心跳", "group": "unassigned", "started_at": now.isoformat(),
                               "updated_at": now.isoformat(), "anomaly_ids": [], "series": [], "routes": [], "anomaly_count": 0}))
    tag_anomalies(es, cfg["indices"]["anomalies"], tags)   # 先寫回異常、再寫件：件寫失敗下一輪會重做；反過來這些異常就永遠沒標到
    es.bulk_index(ALERT_INDEX, docs)
    write_incidents(es, incidents)    # 已被老師傅確認的不覆蓋（原子，不是先讀再寫）
    still = sum(1 for _, d in docs if d["status"] == "open")
    print(f"{now.astimezone(tz):%m-%d %H:%M}：新開 {new} 件、結束 {resolved} 件、進行中 {still} 件")
    return 0

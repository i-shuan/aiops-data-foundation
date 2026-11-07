"""⑨ 值班回饋：命令列的 feedback（pending、add、list、summary、suggest、export）、事故紀錄 incident（add、list、resolve），
與網頁／Grafana 表單走的 submit（一件的回饋）、submit_missed（漏報）、request_rca（重跑根因分析）、list_alerts；計算在 param_suggester.py。
HTTP 層（interface/web）只做收參數、挑回 HTML 還是 JSON。"""
from __future__ import annotations

from shared.config.series import SeriesConfig
import json
import sys
from collections import Counter
from datetime import datetime, timezone

import pandas as pd

import llm.summary as LLM
from shared.infra.env import CODE_ROOT
from shared.config import load_config
from shared.infra.es import ES, doc_id
from learn.store.models import RCA_USEFUL, VERDICTS
from shared.types.incident import Incident
from shared.infra.repos import Repos
from shared.scenario import load_scenario
import detect.series_ops.manual_adjust as A
from detect.series_ops.manual_adjust import load_adjustments
from detect.utils import _now
from detect.strategy.dynamic_baseline import DetectOptions
import learn.param_suggester as F
from learn.param_suggester import propose_from_text


def _anomaly_rows(repo, days: int, now) -> pd.DataFrame:
    """最近 days 天的異常（series、target、status、id、時間）。"""
    rows = [{"id": s["_id"], "series": s["series"], "target": F.target_of(s.get("keys")),
             # 規則、同儕比對、品質檢查沒有 warning／anomaly 之分，都是直接通知的
             "status": s.get("status") or "anomaly", "at": s["@timestamp"]}
            for s in repo.anomalies.between(now - pd.Timedelta(days=days), now, ["@timestamp", "series", "keys", "status", "severity", "level"])]
    return pd.DataFrame(rows, columns=["id", "series", "target", "status", "at"])

def _feedback_rows(repo, days: int, now) -> pd.DataFrame:
    rows = [{"series": f["series"], "target": F.target_of(f.get("keys")), "verdict": f["verdict"], "anomaly_id": f.get("anomaly_id"),
             "at": f["@timestamp"], "by": f.get("by"), "note": f.get("note"), "fp_reason": f.get("fp_reason")}
            for f in repo.feedback.since(now - pd.Timedelta(days=days))]
    return pd.DataFrame(rows, columns=["series", "target", "verdict", "anomaly_id", "at", "by", "note", "fp_reason"])

def _confirmed_incidents(repo, days: int, now) -> list[dict]:
    """老師傅確認過、有根因分類的事故（升級成規則的材料）。"""
    return [{"id": i, **src} for i, src in repo.incidents.find(("confirmed", "corrected"), since=now - pd.Timedelta(days=days),
                                                                 having=("root_cause_category",))]


def _anomaly_details(repo, ids: list[str]) -> list[dict]:
    """被標成誤報的那幾筆異常：時間、值、平常值、z（suggest 依這些選放寬方式）。"""
    if not ids:
        return []
    return [{"at": g.get("@timestamp"), "value": g.get("value"), "median": g.get("median"), "z": g.get("z"), "n": g.get("n"), "keys": g.get("keys")}
            for g in repo.anomalies.get_many(ids, ["@timestamp", "value", "median", "z", "n", "keys"]).values()]


def _deploy_before(repo, fps: list[dict], grace_min: int = 10):
    """這些誤報的對象在它們之前 grace_min 分鐘內有沒有換版（情境的 change 策略：IT 看版號、工廠看 recipe 變更）；查不到就 None。"""
    sc = repo.scenario
    svc = next((v for f in fps for k in sc.entity_keys if (v := (f.get("keys") or {}).get(k))), None)
    ats = [pd.Timestamp(f["at"]) for f in fps if f.get("at")]
    if not svc or not ats:
        return None
    try:
        return sc.change(repo, {}, str(svc), (min(ats) - pd.Timedelta(minutes=grace_min)).to_pydatetime(),
                                          (max(ats) + pd.Timedelta(minutes=1)).to_pydatetime())
    except Exception:  # noqa: BLE001  沒有 service.version、來源不是 ES：當作沒換版
        return None


def _anomalies_of(repo, incidents: list[dict]) -> dict[str, list[dict]]:
    """每件事故 anomaly_ids 對到的異常（series、value、keys）：規則草稿的門檻從這裡算。"""
    ids = sorted({a for i in incidents for a in (i.get("anomaly_ids") or [])[:200]})
    if not ids:
        return {}
    src = repo.anomalies.get_many(ids, ["series", "value", "keys"])
    return {i["id"]: [src[a] for a in (i.get("anomaly_ids") or []) if a in src] for i in incidents}


EXPORT_COLUMNS = ("feedback_at", "verdict", "fp_reason", "by", "note", "alert_id", "anomaly_id", "anomaly_at", "series", "value", "median", "z",
                  "level", "status", "severity", "source", "source_id", "keys")


def export_rows(repo, days: int, now) -> list[dict]:
    """最近 days 天的每筆回饋＋它那筆異常的內容（值、分數、來源識別碼、對象）：一列一筆，給 ML 團隊當標記重訓
    （工廠版 engine 調不到 ML 的門檻，誤報只能回給模型那邊；docs/pdca/meta/2026-11-factory-engine.md）。"""
    fbs = repo.feedback.since(now - pd.Timedelta(days=days))
    ids = sorted({f["anomaly_id"] for f in fbs if f.get("anomaly_id")})
    anoms = repo.anomalies.get_many(ids, ["@timestamp", "series", "keys", "value", "median", "z", "level", "status", "severity", "source", "source_id"])
    out = []
    for f in fbs:
        a = anoms.get(f.get("anomaly_id") or "", {})
        out.append({"feedback_at": f.get("@timestamp"), "verdict": f.get("verdict"), "fp_reason": f.get("fp_reason"), "by": f.get("by"),
                    "note": f.get("note"), "alert_id": f.get("alert_id"), "anomaly_id": f.get("anomaly_id"),
                    "anomaly_at": a.get("@timestamp") or f.get("anomaly_at"), "series": a.get("series") or f.get("series"),
                    "value": a.get("value"), "median": a.get("median"), "z": a.get("z"), "level": a.get("level"), "status": a.get("status"),
                    "severity": a.get("severity"), "source": a.get("source"), "source_id": a.get("source_id"),
                    "keys": a.get("keys") or f.get("keys") or {}})
    return out


def write_export(rows: list[dict], out: str | None) -> str:
    """寫 .ndjson（keys 保留巢狀）或 .csv（keys 攤成 key.<名> 欄）；out 空的印到 stdout（NDJSON）。回傳寫到哪。"""
    if out and out.lower().endswith(".csv"):
        flat = [{**{k: v for k, v in r.items() if k != "keys"}, **{f"key.{k}": v for k, v in (r.get("keys") or {}).items()}} for r in rows]
        pd.DataFrame(flat).to_csv(out, index=False, encoding="utf-8-sig")
        return out
    lines = [json.dumps(r, ensure_ascii=False, default=str) for r in rows]
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        return out
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    return "stdout"


def _connect() -> Repos:
    repo = Repos(ES(), load_scenario())
    repo.feedback.ensure()
    return repo


def _timezone(series_config: SeriesConfig) -> str:
    return series_config.train.timezone or "Asia/Taipei"


# ── feedback 的六個子命令：每個一個函式，(series_config, args) 進、回傳碼出（feedback/cli.py 掛成 python3 -m main detector feedback <子命令>）──

def feedback_export(series_config: SeriesConfig, args) -> int:
    """回饋＋異常合成一列匯出（NDJSON／CSV），給 ML 團隊重訓（工廠版）。"""
    repo, now = _connect(), _now(None)
    rows = export_rows(repo, args.days, now)
    where = write_export(rows, getattr(args, "out", None))
    if where != "stdout":
        c = Counter(r["verdict"] for r in rows)
        print(f"匯出 {len(rows)} 筆回饋（最近 {args.days} 天）→ {where}；" + "、".join(f"{k} {v}" for k, v in sorted(c.items())))
    return 0


def feedback_pending(series_config: SeriesConfig, args) -> int:
    """還沒人標記的異常（status anomaly、沒有回饋的）。"""
    repo, now = _connect(), _now(None)
    a, f = _anomaly_rows(repo, args.days, now), _feedback_rows(repo, args.days, now)
    done = set(f["anomaly_id"].dropna())
    a = a[(a["status"] == "anomaly") & ~a["id"].isin(done)]
    if args.series:
        a = a[a["series"].str.contains(args.series)]
    print(f"最近 {args.days} 天還沒人標記的異常 {len(a)} 筆（顯示最新 {args.limit} 筆）：")
    for r in a.sort_values("at", ascending=False).head(args.limit).to_dict("records"):
        print(f"  {r['id']}  {r['at'][:16]}  {r['series']}  {r['target']}")
    return 0


def feedback_list(series_config: SeriesConfig, args) -> int:
    repo, now = _connect(), _now(None)
    f = _feedback_rows(repo, args.days, now)
    for r in f.sort_values("at", ascending=False).head(args.limit).to_dict("records"):
        print(f"  {r['at'][:16]}  {r['verdict']:<16}  {r['series']}  {r['target']}  ({r['by'] or '?'}) {r['note'] or ''}")
    print(f"共 {len(f)} 筆")
    return 0


def feedback_summary(series_config: SeriesConfig, args) -> int:
    """誤報統計（suggest.summarize）。"""
    repo, now = _connect(), _now(None)
    a, f = _anomaly_rows(repo, args.days, now), _feedback_rows(repo, args.days, now)
    print("\n".join(F.format_summary(F.summarize(a, f, min_false_positive=args.min_false_positive), args.days)))
    return 0


# ── suggest：四種建議各一個函式；寫進 aiops-tuning 的動作只有 _propose 一處 ──

def _override_key(series, match) -> tuple:
    return (series, json.dumps(match, sort_keys=True))


def _proposed(repo, _id: str) -> bool:
    """這筆建議已經在 aiops-tuning 了（同 id 不重提）。"""
    return repo.tuning.exists(_id)


def _propose(repo, _id: str, sug: dict, now, text: str) -> None:
    """寫一筆建議（status 由 suggest_* 給，都是 proposed）並印出來。"""
    sug.update({"at": now.isoformat(), "by": "feedback"})
    repo.tuning.ensure()
    repo.tuning.put(_id, sug)
    print(f"  建議 {_id}{text}")


def _suggest_false_positives(repo, series_config: SeriesConfig, args, s: dict, z0: float, overrides: dict, now) -> int:
    """有人標誤報就提（不用累積）：特徵全部算完再查表，老師傅選的原因優先（suggest.suggest_for_false_positives）。"""
    fb = _feedback_rows(repo, args.days, now)
    tz, specs = _timezone(series_config), {sp["name"]: sp for sp in series_config.series}
    n = 0
    for r in s["candidates"].to_dict("records"):
        if str(r["series"]).startswith("rule."):   # 靜態規則沒有 z、min_abs：門檻在 rules.yaml，這裡提不了
            print(f"  靜態規則的誤報（{r['series']} {r['target']}）：改 rules.yaml 的門檻或 for，這裡不提")
            continue
        rows = fb[(fb["verdict"] == "false_positive") & (fb["series"] == r["series"]) & (fb["target"] == r["target"])]
        fps = _anomaly_details(repo, sorted(set(rows["anomaly_id"].dropna())))
        spec = specs.get(r["series"]) or {}
        sugs = F.suggest_for_false_positives(r["series"], r["target"], fps, z0, float(spec.get("min_abs", 0.0)), tz,
                                             reasons=rows["fp_reason"].dropna().tolist(), current_min_count=spec.get("min_count"),
                                             deploy_at=_deploy_before(repo, fps))
        for sug in sugs:
            what = sug.get("kind", "override") + ":" + ",".join(k for k in ("z", "min_abs", "min_count", "reset_at", "start") if sug.get(k) is not None) \
                + json.dumps(sug.get("effect") or {}, sort_keys=True)
            _id = doc_id("suggest", sug["series"], sug["match"], what)
            if _override_key(sug["series"], sug["match"]) in overrides and sug.get("kind", "override") == "override" and sug.get("z"):
                print(f"  已經有 z 的調整：{r['series']} {r['target']}")
                continue
            if _proposed(repo, _id):
                print(f"  已經提過：{r['series']} {r['target']}（{_id}）")
                continue
            _propose(repo, _id, sug, now, f"：{A.describe(sug)}")
            n += 1
    return n


def _suggest_missed(repo, s: dict, z0: float, overrides: dict, now) -> int:
    """漏報：那條序列、那個對象的 z 降 0.8 倍（proposed，回測看抓不抓得到）。
    這個對象已經有調整（例如之前誤報被放寬成 z 7）：從那個值往下降，不是從設定檔的 z；漏報不能因為以前放寬過就沒建議。"""
    n = 0
    for r in s["missed"].to_dict("records"):
        sug0 = F.suggest_missed(r["series"], r["target"], z0)
        cur = overrides.get(_override_key(sug0["series"], sug0["match"]))
        sug = F.suggest_missed(r["series"], r["target"], float(cur["z"])) if cur else sug0
        _id = doc_id("suggest-missed", sug["series"], sug["match"])
        if _proposed(repo, _id):
            print(f"  漏報的建議已經有了：{r['series']} {r['target']}（{_id}）")
            continue
        if cur:
            sug["reason"] += f"（這個對象現在是 {cur.get('status')} 的 z {float(cur['z']):g}：{cur.get('reason') or ''}）"
            print(f"  注意：{r['series']} {r['target']} 之前被放寬過（z {float(cur['z']):g}），漏報的建議從它往下降")
        _propose(repo, _id, sug, now, f"（漏報）：{A.describe(sug)}")
        n += 1
    return n


def _suggest_precision(repo, s: dict, z0: float, now) -> int:
    """整條序列的 precision 太低／太高：放寬／收緊整條（docs/pdca/meta/2026-11-case-classify.md）。"""
    n = 0
    for sug in F.suggest_by_precision(s["series"], z0):
        _id = doc_id("suggest-precision", sug["series"])
        if _proposed(repo, _id):
            print(f"  precision 的建議已經有了：{sug['series']}（{_id}）")
            continue
        _propose(repo, _id, sug, now, f"（precision）：{sug['reason']}")
        n += 1
    return n


def _suggest_rules(repo, series_config: SeriesConfig, args, now) -> int:
    """同一個服務、同一個根因分類被確認 ≥ N 次 → 提「升級成規則」（不用等 baseline，一次就報）。"""
    confirmed = _confirmed_incidents(repo, args.days, now)
    n = 0
    for sug in F.suggest_rules(confirmed, args.min_recurring, _anomalies_of(repo, confirmed), series_config.series):
        _id = doc_id("suggest-rule", sug["service"], sug["root_cause_category"])
        if _proposed(repo, _id):
            continue
        _propose(repo, _id, sug, now, f"（升級成規則）：{sug['reason']}")
        n += 1
    return n


def feedback_suggest(series_config: SeriesConfig, args) -> int:
    """誤報多次→放寬、漏報→收緊、整條序列 precision、同根因 ≥ N 次→規則；全部 proposed，人核准（adjust approve）才生效。"""
    repo, now = _connect(), _now(None)
    s = F.summarize(_anomaly_rows(repo, args.days, now), _feedback_rows(repo, args.days, now), min_false_positive=args.min_false_positive)
    z0 = DetectOptions(**series_config.detect.to_dict()).z
    overrides = {_override_key(a.get("series"), a.get("match")): a for a in load_adjustments(repo, series_config)
                 if (a.get("kind") or "override") == "override" and a.get("z")}
    n = (_suggest_false_positives(repo, series_config, args, s, z0, overrides, now) + _suggest_missed(repo, s, z0, overrides, now)
         + _suggest_precision(repo, s, z0, now) + _suggest_rules(repo, series_config, args, now))
    print(f"{n} 筆建議（status: proposed；override 先 backtest --id … 再 adjust approve --id …；規則草稿照 rule_draft 加進 rules.yaml）；"
          f"誤報不到 {args.min_false_positive} 次的不提")
    return 0


# ── add ──

def _target(repo, series_config: SeriesConfig, args, doc: dict) -> int | None:
    """回饋對到哪一筆：--anomaly 的那筆異常，或漏報（--series、--match、--at）。回傳碼不是 None 就是參數不對。"""
    if args.anomaly:
        src = repo.anomalies.get(args.anomaly)
        if src is None:
            print(f"找不到異常 {args.anomaly}（feedback pending 看得到 id）")
            return 2
        doc.update(anomaly_id=args.anomaly, anomaly_at=src["@timestamp"], series=src["series"], keys=src.get("keys") or {})
        return None
    if args.verdict == "missed" and args.series:
        at = pd.Timestamp(args.at)
        doc.update(series=args.series, keys=dict(m.split("=", 1) for m in (args.match or [])),
                   anomaly_at=(at.tz_localize(_timezone(series_config)) if at.tzinfo is None else at).isoformat())
        return None
    print("要給 --anomaly <id>；漏報（missed）給 --series、--match、--at")
    return 2


def _extract(repo, series_config: SeriesConfig, args, doc: dict, alert: dict | None, now) -> dict:
    """--text 一句自然語言：抽成四欄（core.llm.extract_feedback），原文與抽到的都存；對得到調整種類就寫一筆 proposed 的 aiops-tuning
    （人核准才生效）。抽不出來只存原文，回饋照寫。"""
    ctx = alert or {"alert_id": args.incident, "service": A.key_value(doc.get("keys") or {}, "service"), "series": [doc.get("series")]}
    ext = LLM.extract_feedback(args.text, ctx)
    doc["feedback_text"] = args.text
    doc["extract"] = {k: v for k, v in {**(ext["fields"] or {}), "ok": ext["ok"], "provider": ext["provider"], "reason": ext["reason"]}.items() if v not in (None, "")}
    print(f"抽取（{ext['provider']}）：" + (json.dumps(ext["fields"], ensure_ascii=False) if ext["ok"] else f"沒抽到（{ext['reason']}），只存原文"))
    if ext["ok"]:
        doc["tuning_id"] = propose_from_text(repo, ext["fields"], ctx, args.text, args.by, now, DetectOptions(**series_config.detect.to_dict()).z)
    return ext


def feedback_add(series_config: SeriesConfig, args) -> int:
    """值班對一筆異常（或漏報）的判斷：真的／誤報／根因錯／漏報；--text 一句話讓系統抽。"""
    repo, now = _connect(), _now(None)
    if not args.verdict and not args.text:
        print(f"要給 --verdict（{'、'.join(VERDICTS)}），或 --text 一句話讓系統抽")
        return 2
    doc = {"@timestamp": now.isoformat(), "note": args.note, "by": args.by, "incident_id": args.incident, "correct_root_cause": args.root_cause}
    rc = _target(repo, series_config, args, doc)
    if rc is not None:
        return rc
    alert = None
    if args.alert:
        alert = repo.alerts.get(args.alert)
        if alert is None:
            print(f"找不到告警 {args.alert}")
            return 2
        doc["alert_id"] = alert["alert_id"]
    ext = _extract(repo, series_config, args, doc, alert, now) if args.text else None
    # 沒給 --verdict 才用抽出來的，而且只信真的模型：關鍵字退路（template）會把「是供應商的問題、不是我們的程式」判成誤報（eval 實測），
    # 讓它決定 verdict 會寫錯資料；退路抽的只存在 extract 裡給人看
    from_model = bool(ext and ext["ok"] and not ext["provider"].startswith("template"))
    verdict = args.verdict or (ext["fields"]["feedback_type"] if from_model else None)
    if verdict not in VERDICTS:
        why = ("（一句話是關鍵字退路抽的，不拿來當 verdict）" if ext and ext["ok"] else "（一句話沒抽出 feedback_type）") if args.text and not args.verdict else ""
        print(f"--verdict 要是 {'、'.join(VERDICTS)}{why}")
        return 2
    doc["verdict"] = verdict
    doc = {k: v for k, v in doc.items() if v not in (None, "")}
    _id = doc_id("feedback", doc.get("anomaly_id") or [doc["series"], doc.get("keys"), doc.get("anomaly_at")], args.by or "")
    repo.feedback.put(_id, doc)   # 同一個人對同一筆再標記會覆蓋
    print(f"已記錄：{verdict}  {doc['series']}  {F.target_of(doc.get('keys'))}")
    if doc.get("tuning_id"):
        print(f"  依這句話提了一筆調整建議 {doc['tuning_id']}（status proposed；adjust list 看得到，adjust approve 才生效）")
    return 0


# ── incident 的三個子命令 ──

def _incidents() -> Repos:
    repo = Repos(ES(), load_scenario())
    repo.incidents.ensure()
    return repo


def incident_list(series_config: SeriesConfig, args) -> int:
    hits = _incidents().incidents.recent(200)
    for _id, i in hits:
        print(f"  {_id}  [{i.get('status')}] {i['start'][:16]}～{i['end'][:16]}  {i.get('title')}  "
              f"服務={','.join(i.get('services') or []) or '—'}  根因={i.get('root_cause') or '—'}")
    print(f"共 {len(hits)} 件")
    return 0


def incident_resolve(series_config: SeriesConfig, args) -> int:
    repo, now = _incidents(), _now(None)
    repo.incidents.patch(args.id, {"status": "resolved", "resolved_at": now.isoformat(), "action": args.action_taken, "root_cause": args.root_cause})
    print(f"已結案：{args.id}")
    return 0


def incident_add(series_config: SeriesConfig, args) -> int:
    """人工補登一件事故；--exclude-training 同時提一筆「這段不學」（proposed）。"""
    repo, now = _incidents(), _now(None)
    if not (args.title and args.start and args.end):
        print("要給 --title、--start、--end")
        return 2
    tz = _timezone(series_config)
    local = lambda x: (pd.Timestamp(x).tz_localize(tz) if pd.Timestamp(x).tzinfo is None else pd.Timestamp(x)).isoformat()   # 沒寫時區的當台北時間
    doc = Incident(**{"@timestamp": now.isoformat()}, title=args.title, start=local(args.start), end=local(args.end),
                   services=args.service or [], severity=args.severity, status="open", root_cause=args.root_cause,
                   action=args.action_taken, by=args.by, anomaly_ids=args.anomaly or []).to_doc()
    _id = args.id or doc_id("incident", doc["title"], doc["start"])
    repo.incidents.put(_id, doc)
    print(f"已記錄事故 {_id}：{args.title}（{doc['start'][:16]}～{doc['end'][:16]}）")
    if args.exclude_training:
        repo.tuning.ensure()
        for sug in F.suggest_exclude({**doc, "id": _id}):
            sid = doc_id("incident-exclude", _id, sug.get("match"))
            repo.tuning.put(sid, {**sug, "at": now.isoformat(), "by": args.by or "incident"})
            print(f"  建議 {sid}：{A.describe(sug)}")
        print("  status 是 proposed，用 adjust approve --id … 核准後，下次 train 才不學這段")
    return 0


# ── 網頁版／Grafana 表單走的（interface/web/routes.py 呼叫；跟命令列版寫同一種文件）──

# 漏報表單的序列選單、時區：跟 detector 讀同一份 series.yaml（image 裡 configs/ 跟套件放在一起，路徵一樣）
_SERIES_CFG = load_config(str(CODE_ROOT / "configs/baseline/series.yaml"), "series")
SERIES_NAMES = [s["name"] for s in _SERIES_CFG["series"]]
TIMEZONE = _SERIES_CFG.get("train", {}).get("timezone", "Asia/Taipei")
WEB_VERDICTS = ("true_positive", "false_positive", "wrong_root_cause")     # 表單能選的（漏報走 submit_missed）
RCA_QUESTIONS = ("rca_cause_useful", "rca_action_useful")                  # 採納率的兩題（docs/pdca/phase5-learning-loop/2026-10-feedback-nl-extract.md）
_ready: set[int] = set()   # 哪些連線已經確認過 index（每個連線一次）


def _ensure(repo: Repos) -> None:
    """index 要先用正式的 mapping 建（不然第一筆寫進去 ES 會自己猜型別，之後 feedback suggest 的 mapping 就合不上）；每個連線只做一次。"""
    if id(repo.es) not in _ready:
        repo.feedback.ensure()
        repo.incidents.ensure()
        _ready.add(id(repo.es))


def _feedback_base(form: dict, alert: dict, verdict: str, by: str, now: datetime) -> dict:
    """一筆回饋共同的欄位（每筆異常各一筆時複製這份）。"""
    base = {"@timestamp": now.isoformat(), "verdict": verdict, "by": by, "alert_id": alert["alert_id"],
            "alert_started_at": alert.get("started_at"), "incident_id": alert["alert_id"],
            "note": (form.get("note") or form.get("fp_reason_text") or "").strip() or None,
            "fp_reason": form.get("fp_reason") if verdict == "false_positive" else None,
            "correct_root_cause": form.get("root_cause") if verdict == "wrong_root_cause" else None}
    for q in RCA_QUESTIONS:
        if form.get(q) in RCA_USEFUL:
            base[q] = form[q]
    return base


def _extract_text(repo: Repos, text: str, alert: dict, by: str, now: datetime, base: dict) -> tuple[str | None, str | None]:
    """一句自然語言 → LLM 抽成四欄（llm/summary.extract_feedback）；驗不過就只存原文。對得到調整種類就先寫一筆 proposed 的 aiops-tuning。
    回 (tuning_id, tuning_error)：建議寫不進去（ES 權限、mapping）不能讓回饋本身失敗。"""
    ext = LLM.extract_feedback(text, alert)
    base["feedback_text"] = text
    base["extract"] = {k: v for k, v in {**(ext["fields"] or {}), "ok": ext["ok"], "provider": ext["provider"], "reason": ext["reason"]}.items() if v not in (None, "")}
    if not ext["ok"]:
        return None, None
    try:
        tuning_id = propose_from_text(repo, ext["fields"], alert, text, by, now)
    except Exception as e:    # noqa: BLE001
        sys.stderr.write(f"feedback tuning proposal failed: {e!r}\n")
        return None, f"{type(e).__name__}：{str(e)[:120]}"
    base["tuning_id"] = tuning_id
    return tuning_id, None


def _feedback_docs(repo: Repos, alert: dict, base: dict, by: str) -> list[tuple[str, dict]]:
    """一件的每筆異常各一筆回饋：現有的 feedback summary／suggest 是依異常（series＋對象）算的，才對得起來。
    異常已經不在了（過期）：至少留一筆對這件的。"""
    ids = alert.get("anomaly_ids") or []
    got = repo.anomalies.get_many(ids[:200], ["@timestamp", "series", "keys"]) if ids else {}
    docs = [(doc_id("feedback", aid, by), {**base, "anomaly_id": aid, "anomaly_at": src.get("@timestamp"), "series": src.get("series"), "keys": src.get("keys") or {}})
            for aid, src in got.items()]
    if not docs:
        docs.append((doc_id("feedback", alert["alert_id"], by), {**base, "series": (alert.get("series") or ["?"])[0], "keys": {"service": alert.get("service")}}))
    return docs


def _incident_patch(form: dict, verdict: str, by: str, now: datetime) -> dict:
    """事故要改的欄位：確認／更正／誤報；誤報不填根因那幾欄。"""
    status = {"true_positive": "confirmed", "wrong_root_cause": "corrected", "false_positive": "false_positive"}[verdict]
    inc = {"status": status, "confirmed_at": now.isoformat(), "confirmed_by": by, "by": by}
    if verdict != "false_positive":
        inc.update({"root_cause_category": form.get("root_cause_category") or None, "root_cause": (form.get("root_cause") or "").strip() or None,
                    "action": (form.get("action") or "").strip() or None, "how_detected": (form.get("how_detected") or "").strip() or None,
                    "checked": [c for c in (form.get("checked") or []) if c] or None, "improve": (form.get("improve") or "").strip() or None})
    return {k: v for k, v in inc.items() if v is not None}


def _upsert_incident(repo: Repos, alert: dict, inc: dict, now: datetime) -> None:
    """事故可能還沒被 alert 開出來（例如回饋得很快）：用 upsert 補一筆最小的。"""
    ids = alert.get("anomaly_ids") or []
    seed = Incident(**{"@timestamp": now.isoformat()}, alert_id=alert["alert_id"], title=alert.get("title"), start=alert.get("started_at"),
                    end=alert.get("resolved_at") or alert.get("updated_at"), services=[alert.get("service")], severity=alert.get("severity"),
                    dept=alert.get("dept"), group=alert.get("group"), anomaly_ids=ids[:Incident.MAX_ANOMALY_IDS], **inc).to_doc()
    repo.incidents.patch(alert["alert_id"], inc, upsert=seed)


def submit(repo: Repos, form: dict, now: datetime | None = None) -> dict:
    """一件的回饋。form：alert_id、verdict、by、root_cause_category、root_cause、action、how_detected、checked（list）、improve、fp_reason、note、
    feedback_text（一句自然語言，選填）、rca_cause_useful、rca_action_useful（yes／no／not_seen，選填）。
    回傳 {"feedback": 寫了幾筆, "incident": 狀態, "by"}；有給 feedback_text 時多 "extract"（抽出來的四欄＋ok、provider、reason）
    和 "tuning_id"（寫進 aiops-tuning 的建議 id；suggested_action 是 none、或沒抽到就是 None）。
    抽取失敗不影響寫入：只存原文（extract.ok false）；寫建議失敗也不影響回饋本身（tuning_error）。
    找不到這件 → KeyError；verdict 不對 → ValueError。"""
    now = now or datetime.now(timezone.utc)
    alert = repo.alerts.get(form["alert_id"])
    if not alert:
        raise KeyError(f"找不到告警 {form['alert_id']}")
    verdict = form["verdict"]
    if verdict not in WEB_VERDICTS:
        raise ValueError(f"verdict 要是 {'、'.join(WEB_VERDICTS)}")
    by = (form.get("by") or "").strip() or "anonymous"
    _ensure(repo)
    base = _feedback_base(form, alert, verdict, by, now)
    text = (form.get("feedback_text") or "").strip()
    tuning_id, tuning_error = _extract_text(repo, text, alert, by, now, base) if text else (None, None)
    base = {k: v for k, v in base.items() if v not in (None, "")}
    docs = _feedback_docs(repo, alert, base, by)
    repo.feedback.bulk(docs)
    inc = _incident_patch(form, verdict, by, now)
    _upsert_incident(repo, alert, inc, now)
    out = {"feedback": len(docs), "incident": inc["status"], "by": by}
    if text:
        out.update({"extract": base.get("extract"), "tuning_id": tuning_id, **({"tuning_error": tuning_error} if tuning_error else {})})
    return out


def submit_missed(repo: Repos, form: dict, now: datetime | None = None) -> dict:
    """漏報（該報卻沒報）：沒有 alert_id、沒有 anomaly_id，寫一筆 verdict: missed。
    form：series（series.yaml 裡的名字）、service、at（ISO 或 YYYY-MM-DD HH:MM；沒時區當 series.yaml 的時區）、by、note、route／pod（選填，進 keys）。
    文件格式、_id 跟命令列 feedback add --verdict missed 一樣，同一個人對同一條再送會覆蓋、不重複。參數不對 → ValueError。"""
    now = now or datetime.now(timezone.utc)
    series = (form.get("series") or "").strip()
    if series not in SERIES_NAMES:
        raise ValueError(f"series 要是 {'、'.join(SERIES_NAMES)}")
    service = (form.get("service") or "").strip()
    if not service:
        raise ValueError("service 要填（哪個服務該報卻沒報）")
    raw_at = (form.get("at") or "").strip()
    if not raw_at:
        raise ValueError("at 要填（什麼時候該報：2026-10-25 10:00）")
    try:
        at = pd.Timestamp(raw_at)
    except (ValueError, TypeError) as e:
        raise ValueError(f"at 看不懂（{raw_at}）：寫 2026-10-25 10:00 或 ISO 格式") from e
    if pd.isna(at):
        raise ValueError("at 要填（什麼時候該報：2026-10-25 10:00）")
    by = (form.get("by") or "").strip() or "anonymous"
    keys = {"service": service, **{k: (form.get(k) or "").strip() for k in ("route", "pod") if (form.get(k) or "").strip()}}
    doc = {"@timestamp": now.isoformat(), "verdict": "missed", "series": series, "keys": keys,
           "anomaly_at": (at.tz_localize(TIMEZONE) if at.tzinfo is None else at).isoformat(), "by": by,
           "note": (form.get("note") or "").strip() or None}
    doc = {k: v for k, v in doc.items() if v not in (None, "")}
    _ensure(repo)
    repo.feedback.put(doc_id("feedback", [doc["series"], doc["keys"], doc["anomaly_at"]], by), doc)
    return {"feedback": 1, "verdict": "missed", "series": series, "service": service, "by": by}


def request_rca(repo: Repos, alert_id: str, now: datetime | None = None) -> dict:
    """在開著的那件上標 rca_requested：下一輪 alert 看到就強制重跑根因分析（含相似事故、LLM、摘要，跟新開件同一條路），跑完把標記拿掉。
    結束的件不重跑（ValueError → 409）；沒有這件 LookupError → 404。"""
    alert = repo.alerts.get(alert_id)
    if not alert:
        raise LookupError(f"找不到告警 {alert_id}")
    if alert.get("status") != "open":
        raise ValueError(f"這件已經結束（{alert.get('status')}），不重跑；要看舊報告用 /report?alert={alert_id}")
    at = (now or datetime.now(timezone.utc)).isoformat()
    repo.alerts.request_rca(alert_id, at)
    return {"alert_id": alert_id, "requested_at": at}


def list_alerts(repo: Repos, status: str = "open", size: int = 50) -> list[dict]:
    """件的清單（新的在前），不含報告全文。status：open／resolved／all。"""
    if status not in ("open", "resolved", "all"):
        raise ValueError("status 要是 open、resolved 或 all")
    return repo.alerts.list(status, max(1, min(int(size), 500)))

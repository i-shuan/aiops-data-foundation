"""Check：事故紀錄 incident、值班回饋 feedback（detector/feedback.py）。"""
from __future__ import annotations

import json

import pandas as pd

from core import llm as LLM
from core import plugin

from .es import ANOMALY_INDEX, ES, doc_id
from . import adjust as A
from . import feedback as F
from .model import DetectOptions
from .common import _now
from .tuning import TUNING_INDEX, TUNING_MAPPING, load_adjustments, propose_from_text


def _anomaly_rows(es, days: int, now) -> pd.DataFrame:
    """最近 days 天的異常（series、target、status、id、時間）。"""
    rows, after = [], None
    while True:
        body = {"size": 5000, "_source": ["@timestamp", "series", "keys", "status", "severity", "level"],
                "query": {"range": {"@timestamp": {"gte": (now - pd.Timedelta(days=days)).isoformat(), "lt": now.isoformat()}}},
                "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}]}
        if after:
            body["search_after"] = after
        hits = es.req("POST", f"/{ANOMALY_INDEX}/_search?ignore_unavailable=true", body).get("hits", {}).get("hits", [])
        rows += [{"id": h["_id"], "series": h["_source"]["series"], "target": F.target_of(h["_source"].get("keys")),
                  # 規則、跟兄弟比、品質檢查沒有 warning／anomaly 之分，都是直接通知的
                  "status": h["_source"].get("status") or "anomaly", "at": h["_source"]["@timestamp"]} for h in hits]
        if len(hits) < 5000:
            break
        after = hits[-1]["sort"]
    return pd.DataFrame(rows, columns=["id", "series", "target", "status", "at"])

def _feedback_rows(es, days: int, now) -> pd.DataFrame:
    hits = es.req("POST", f"/{F.FEEDBACK_INDEX}/_search?ignore_unavailable=true", {
        "size": 10000, "sort": [{"@timestamp": "asc"}],
        "query": {"range": {"@timestamp": {"gte": (now - pd.Timedelta(days=days)).isoformat()}}}}).get("hits", {}).get("hits", [])
    return pd.DataFrame([{"series": h["_source"]["series"], "target": F.target_of(h["_source"].get("keys")),
                          "verdict": h["_source"]["verdict"], "anomaly_id": h["_source"].get("anomaly_id"),
                          "at": h["_source"]["@timestamp"], "by": h["_source"].get("by"), "note": h["_source"].get("note"),
                          "fp_reason": h["_source"].get("fp_reason")}
                         for h in hits], columns=["series", "target", "verdict", "anomaly_id", "at", "by", "note", "fp_reason"])

def _confirmed_incidents(es, days: int, now) -> list[dict]:
    """老師傅確認過、有根因分類的事故（升級成規則的材料）。"""
    hits = es.req("POST", f"/{F.INCIDENT_INDEX}/_search?ignore_unavailable=true", {
        "size": 1000, "query": {"bool": {"filter": [{"terms": {"status": ["confirmed", "corrected"]}}, {"exists": {"field": "root_cause_category"}},
                                                    {"range": {"start": {"gte": (now - pd.Timedelta(days=days)).isoformat()}}}]}}}).get("hits", {}).get("hits", [])
    return [{"id": h["_id"], **h["_source"]} for h in hits]


def _anomaly_details(es, ids: list[str]) -> list[dict]:
    """被標成誤報的那幾筆異常：時間、值、平常值、z（suggest 依這些選放寬方式）。"""
    if not ids:
        return []
    got = es.req("POST", f"/{ANOMALY_INDEX}/_mget?_source_includes=@timestamp,value,median,z,n,keys", {"ids": ids}).get("docs", [])
    return [{"at": g["_source"].get("@timestamp"), "value": g["_source"].get("value"), "median": g["_source"].get("median"),
             "z": g["_source"].get("z"), "n": g["_source"].get("n"), "keys": g["_source"].get("keys")} for g in got if g.get("found")]


def _deploy_before(es, fps: list[dict], grace_min: int = 10):
    """這些誤報的對象在它們之前 grace_min 分鐘內有沒有換版（plugin 的 change_at：IT 看版號、工廠看 recipe 變更）；查不到就 None。"""
    svc =next((v for f in fps for k in plugin.current().entity_keys if (v := (f.get("keys") or {}).get(k))), None)
    ats = [pd.Timestamp(f["at"]) for f in fps if f.get("at")]
    if not svc or not ats:
        return None
    try:
        return plugin.current().change_at(es, {}, str(svc), (min(ats) - pd.Timedelta(minutes=grace_min)).to_pydatetime(),
                                          (max(ats) + pd.Timedelta(minutes=1)).to_pydatetime())
    except Exception:  # noqa: BLE001  沒有 service.version、來源不是 ES：當作沒換版
        return None


def _anomalies_of(es, incidents: list[dict]) -> dict[str, list[dict]]:
    """每件事故 anomaly_ids 對到的異常（series、value、keys）：規則草稿的門檻從這裡算。"""
    ids = sorted({a for i in incidents for a in (i.get("anomaly_ids") or [])[:200]})
    if not ids:
        return {}
    got = es.req("POST", f"/{ANOMALY_INDEX}/_mget?_source_includes=series,value,keys", {"ids": ids}).get("docs", [])
    src = {g["_id"]: g["_source"] for g in got if g.get("found")}
    return {i["id"]: [src[a] for a in (i.get("anomaly_ids") or []) if a in src] for i in incidents}


EXPORT_COLUMNS = ("feedback_at", "verdict", "fp_reason", "by", "note", "alert_id", "anomaly_id", "anomaly_at", "series", "value", "median", "z",
                  "level", "status", "severity", "source", "source_id", "keys")


def export_rows(es, days: int, now) -> list[dict]:
    """最近 days 天的每筆回饋＋它那筆異常的內容（值、分數、來源識別碼、對象）：一列一筆，給 ML 團隊當標記重訓
    （工廠版 engine 調不到 ML 的門檻，誤報只能回給模型那邊；docs/pdca/meta/2026-11-factory-engine.md）。"""
    hits = es.req("POST", f"/{F.FEEDBACK_INDEX}/_search?ignore_unavailable=true", {
        "size": 10000, "sort": [{"@timestamp": "asc"}],
        "query": {"range": {"@timestamp": {"gte": (now - pd.Timedelta(days=days)).isoformat()}}}}).get("hits", {}).get("hits", [])
    fbs = [h["_source"] for h in hits]
    ids = sorted({f["anomaly_id"] for f in fbs if f.get("anomaly_id")})
    anoms: dict[str, dict] = {}
    for i in range(0, len(ids), 1000):
        got = es.req("POST", f"/{ANOMALY_INDEX}/_mget?_source_includes=@timestamp,series,keys,value,median,z,level,status,severity,source,source_id",
                     {"ids": ids[i:i + 1000]}).get("docs", [])
        anoms.update({g["_id"]: g["_source"] for g in got if g.get("found")})
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
    import sys
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


def cmd_feedback(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(F.FEEDBACK_INDEX, F.FEEDBACK_MAPPING)
    now = _now(None)
    days = args.days
    if args.action == "export":
        rows = export_rows(es, days, now)
        where = write_export(rows, getattr(args, "out", None))
        if where != "stdout":
            from collections import Counter
            c = Counter(r["verdict"] for r in rows)
            print(f"匯出 {len(rows)} 筆回饋（最近 {days} 天）→ {where}；" + "、".join(f"{k} {v}" for k, v in sorted(c.items())))
        return 0
    if args.action == "pending":
        a, f = _anomaly_rows(es, days, now), _feedback_rows(es, days, now)
        done = set(f["anomaly_id"].dropna())
        a = a[(a["status"] == "anomaly") & ~a["id"].isin(done)]
        if args.series:
            a = a[a["series"].str.contains(args.series)]
        print(f"最近 {days} 天還沒人標記的異常 {len(a)} 筆（顯示最新 {args.limit} 筆）：")
        for r in a.sort_values("at", ascending=False).head(args.limit).to_dict("records"):
            print(f"  {r['id']}  {r['at'][:16]}  {r['series']}  {r['target']}")
        return 0
    if args.action == "list":
        f = _feedback_rows(es, days, now)
        for r in f.sort_values("at", ascending=False).head(args.limit).to_dict("records"):
            print(f"  {r['at'][:16]}  {r['verdict']:<16}  {r['series']}  {r['target']}  ({r['by'] or '?'}) {r['note'] or ''}")
        print(f"共 {len(f)} 筆")
        return 0
    if args.action == "summary":
        a, f = _anomaly_rows(es, days, now), _feedback_rows(es, days, now)
        print("\n".join(F.format_summary(F.summarize(a, f, min_false_positive=args.min_false_positive), days)))
        return 0
    if args.action == "suggest":
        s = F.summarize(_anomaly_rows(es, days, now), _feedback_rows(es, days, now), min_false_positive=args.min_false_positive)
        z0 = DetectOptions(**cfg.get("detect", {})).z
        overrides = {(a.get("series"), json.dumps(a.get("match"), sort_keys=True)): a for a in load_adjustments(es, cfg)
                     if (a.get("kind") or "override") == "override" and a.get("z")}
        existing = set(overrides)
        n = 0
        fb = _feedback_rows(es, days, now)
        tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
        specs = {sp["name"]: sp for sp in cfg.get("series", [])}
        # 有人標誤報就提（不用累積）：特徵全部算完再查表，老師傅選的原因優先（feedback.suggest_for_false_positives）
        for r in s["candidates"].to_dict("records"):
            if str(r["series"]).startswith("rule."):   # 靜態規則沒有 z、min_abs：門檻在 rules.yaml，這裡提不了
                print(f"  靜態規則的誤報（{r['series']} {r['target']}）：改 rules.yaml 的門檻或 for，這裡不提")
                continue
            rows = fb[(fb["verdict"] == "false_positive") & (fb["series"] == r["series"]) & (fb["target"] == r["target"])]
            fps = _anomaly_details(es, sorted(set(rows["anomaly_id"].dropna())))
            spec = specs.get(r["series"]) or {}
            sugs = F.suggest_for_false_positives(r["series"], r["target"], fps, z0, float(spec.get("min_abs", 0.0)), tz,
                                                 reasons=rows["fp_reason"].dropna().tolist(), current_min_count=spec.get("min_count"),
                                                 deploy_at=_deploy_before(es, fps))
            for sug in sugs:
                what = sug.get("kind", "override") + ":" + ",".join(k for k in ("z", "min_abs", "min_count", "reset_at", "start") if sug.get(k) is not None) \
                    + json.dumps(sug.get("effect") or {}, sort_keys=True)
                _id = doc_id("suggest", sug["series"], sug["match"], what)
                if (sug["series"], json.dumps(sug["match"], sort_keys=True)) in existing and sug.get("kind", "override") == "override" and sug.get("z"):
                    print(f"  已經有 z 的調整：{r['series']} {r['target']}")
                    continue
                if es.req("GET", f"/{TUNING_INDEX}/_doc/{_id}", ok=(200, 404)).get("found"):
                    print(f"  已經提過：{r['series']} {r['target']}（{_id}）")
                    continue
                sug.update({"at": now.isoformat(), "by": "feedback"})
                es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
                es.req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", sug)
                print(f"  建議 {_id}：{A.describe(sug)}")
                n += 1
        # 漏報：那條序列、那個對象的 z 降 0.8 倍（proposed，回測看抓不抓得到）
        for r in s["missed"].to_dict("records"):
            # 這個對象已經有調整（例如之前誤報被放寬成 z 7）：從那個值往下降，不是從設定檔的 z；漏報不能因為以前放寬過就沒建議
            sug0 = F.suggest_missed(r["series"], r["target"], z0)
            cur = overrides.get((sug0["series"], json.dumps(sug0["match"], sort_keys=True)))
            sug = F.suggest_missed(r["series"], r["target"], float(cur["z"])) if cur else sug0
            _id = doc_id("suggest-missed", sug["series"], sug["match"])
            if es.req("GET", f"/{TUNING_INDEX}/_doc/{_id}", ok=(200, 404)).get("found"):
                print(f"  漏報的建議已經有了：{r['series']} {r['target']}（{_id}）")
                continue
            if cur:
                sug["reason"] += f"（這個對象現在是 {cur.get('status')} 的 z {float(cur['z']):g}：{cur.get('reason') or ''}）"
                print(f"  注意：{r['series']} {r['target']} 之前被放寬過（z {float(cur['z']):g}），漏報的建議從它往下降")
            sug.update({"at": now.isoformat(), "by": "feedback"})
            es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
            es.req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", sug)
            print(f"  建議 {_id}（漏報）：{A.describe(sug)}")
            n += 1
        # 整條序列的 precision 太低／太高：放寬／收緊整條（docs/pdca/meta/2026-11-case-classify.md）
        for sug in F.suggest_by_precision(s["series"], z0):
            _id = doc_id("suggest-precision", sug["series"])
            if es.req("GET", f"/{TUNING_INDEX}/_doc/{_id}", ok=(200, 404)).get("found"):
                print(f"  precision 的建議已經有了：{sug['series']}（{_id}）")
                continue
            sug.update({"at": now.isoformat(), "by": "feedback"})
            es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
            es.req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", sug)
            print(f"  建議 {_id}（precision）：{sug['reason']}")
            n += 1
        # 同一個服務、同一個根因分類被確認 ≥ N 次 → 提「升級成規則」（不用等 baseline，一次就報）
        confirmed = _confirmed_incidents(es, days, now)
        for sug in F.suggest_rules(confirmed, args.min_recurring, _anomalies_of(es, confirmed), cfg.get("series")):
            _id = doc_id("suggest-rule", sug["service"], sug["root_cause_category"])
            got = es.req("GET", f"/{TUNING_INDEX}/_doc/{_id}", ok=(200, 404))
            if got.get("found"):
                continue
            sug.update({"at": now.isoformat(), "by": "feedback"})
            es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
            es.req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", sug)
            print(f"  建議 {_id}（升級成規則）：{sug['reason']}")
            n += 1
        print(f"{n} 筆建議（status: proposed；override 先 backtest --id … 再 adjust approve --id …；規則草稿照 rule_draft 加進 rules.yaml）；"
              f"誤報不到 {args.min_false_positive} 次的不提")
        return 0
    # add
    if not args.verdict and not args.text:
        print(f"要給 --verdict（{'、'.join(F.VERDICTS)}），或 --text 一句話讓系統抽")
        return 2
    doc = {"@timestamp": now.isoformat(), "note": args.note, "by": args.by, "incident_id": args.incident, "correct_root_cause": args.root_cause}
    if args.anomaly:
        got = es.req("GET", f"/{ANOMALY_INDEX}/_doc/{args.anomaly}", ok=(200, 404))
        if not got.get("found"):
            print(f"找不到異常 {args.anomaly}（feedback pending 看得到 id）")
            return 2
        src = got["_source"]
        doc.update(anomaly_id=args.anomaly, anomaly_at=src["@timestamp"], series=src["series"], keys=src.get("keys") or {})
    elif args.verdict == "missed" and args.series:
        tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
        at = pd.Timestamp(args.at)
        doc.update(series=args.series, keys=dict(m.split("=", 1) for m in (args.match or [])),
                   anomaly_at=(at.tz_localize(tz) if at.tzinfo is None else at).isoformat())
    else:
        print("要給 --anomaly <id>；漏報（missed）給 --series、--match、--at")
        return 2
    # 一句自然語言（--text）：抽成四欄（core.llm.extract_feedback）；沒給 --verdict 就用抽出來的 feedback_type；
    # 對得到調整種類就寫一筆 proposed 的 aiops-tuning（人核准才生效）。抽不出來只存原文，回饋照寫
    alert = None
    if args.alert:
        got = es.req("GET", f"/aiops-alerts/_doc/{args.alert}", ok=(200, 404))
        if not got.get("found"):
            print(f"找不到告警 {args.alert}")
            return 2
        alert = got["_source"]
        doc["alert_id"] = alert["alert_id"]
    ext = None
    if args.text:
        ctx = alert or {"alert_id": args.incident, "service": A.key_value(doc.get("keys") or {}, "service"), "series": [doc.get("series")]}
        ext = LLM.extract_feedback(args.text, ctx)
        doc["feedback_text"] = args.text
        doc["extract"] = {k: v for k, v in {**(ext["fields"] or {}), "ok": ext["ok"], "provider": ext["provider"], "reason": ext["reason"]}.items() if v not in (None, "")}
        print(f"抽取（{ext['provider']}）：" + (json.dumps(ext["fields"], ensure_ascii=False) if ext["ok"] else f"沒抽到（{ext['reason']}），只存原文"))
        if ext["ok"]:
            doc["tuning_id"] = propose_from_text(es, ext["fields"], ctx, args.text, args.by, now, DetectOptions(**cfg.get("detect", {})).z)
    # 沒給 --verdict 才用抽出來的，而且只信真的模型：關鍵字退路（template）會把「是供應商的問題、不是我們的程式」判成誤報（eval 實測），
    # 讓它決定 verdict 會寫錯資料；退路抽的只存在 extract 裡給人看
    from_model = bool(ext and ext["ok"] and not ext["provider"].startswith("template"))
    verdict = args.verdict or (ext["fields"]["feedback_type"] if from_model else None)
    if verdict not in F.VERDICTS:
        why = ("（一句話是關鍵字退路抽的，不拿來當 verdict）" if ext and ext["ok"] else "（一句話沒抽出 feedback_type）") if args.text and not args.verdict else ""
        print(f"--verdict 要是 {'、'.join(F.VERDICTS)}{why}")
        return 2
    doc["verdict"] = verdict
    doc = {k: v for k, v in doc.items() if v not in (None, "")}
    _id = doc_id("feedback", doc.get("anomaly_id") or [doc["series"], doc.get("keys"), doc.get("anomaly_at")], args.by or "")
    es.req("PUT", f"/{F.FEEDBACK_INDEX}/_doc/{_id}?refresh=true", doc)   # 同一個人對同一筆再標記會覆蓋
    print(f"已記錄：{verdict}  {doc['series']}  {F.target_of(doc.get('keys'))}")
    if doc.get("tuning_id"):
        print(f"  依這句話提了一筆調整建議 {doc['tuning_id']}（status proposed；adjust list 看得到，adjust approve 才生效）")
    return 0

def cmd_incident(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(F.INCIDENT_INDEX, F.INCIDENT_MAPPING)
    now = _now(None)
    if args.action == "list":
        hits = es.req("POST", f"/{F.INCIDENT_INDEX}/_search?ignore_unavailable=true",
                      {"size": 200, "sort": [{"start": "desc"}]}).get("hits", {}).get("hits", [])
        for h in hits:
            i = h["_source"]
            print(f"  {h['_id']}  [{i.get('status')}] {i['start'][:16]}～{i['end'][:16]}  {i.get('title')}  "
                  f"服務={','.join(i.get('services') or []) or '—'}  根因={i.get('root_cause') or '—'}")
        print(f"共 {len(hits)} 件")
        return 0
    if args.action == "resolve":
        es.req("POST", f"/{F.INCIDENT_INDEX}/_update/{args.id}?refresh=true",
               {"doc": {"status": "resolved", "resolved_at": now.isoformat(), "action": args.action_taken,
                        "root_cause": args.root_cause}})
        print(f"已結案：{args.id}")
        return 0
    if not (args.title and args.start and args.end):
        print("要給 --title、--start、--end")
        return 2
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    local = lambda x: (pd.Timestamp(x).tz_localize(tz) if pd.Timestamp(x).tzinfo is None else pd.Timestamp(x)).isoformat()   # 沒寫時區的當台北時間
    doc = {"@timestamp": now.isoformat(), "title": args.title, "start": local(args.start),
           "end": local(args.end), "services": args.service or [], "severity": args.severity,
           "status": "open", "root_cause": args.root_cause, "action": args.action_taken, "by": args.by,
           "anomaly_ids": args.anomaly or []}
    doc = {k: v for k, v in doc.items() if v not in (None, "", [])}
    _id = args.id or doc_id("incident", doc["title"], doc["start"])
    es.req("PUT", f"/{F.INCIDENT_INDEX}/_doc/{_id}?refresh=true", doc)
    print(f"已記錄事故 {_id}：{args.title}（{doc['start'][:16]}～{doc['end'][:16]}）")
    if args.exclude_training:
        es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
        for sug in F.suggest_exclude({**doc, "id": _id}):
            sid = doc_id("incident-exclude", _id, sug.get("match"))
            es.req("PUT", f"/{TUNING_INDEX}/_doc/{sid}?refresh=true", {**sug, "at": now.isoformat(), "by": args.by or "incident"})
            print(f"  建議 {sid}：{A.describe(sug)}")
        print("  status 是 proposed，用 adjust approve --id … 核准後，下次 train 才不學這段")
    return 0

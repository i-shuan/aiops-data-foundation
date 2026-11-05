"""metrics：量化（docs/pdca/phase5-learning-loop/2026-10-metrics-kpi.md）。一段時間的數字，兩段並排就是「學之前 vs 學之後」。

讀 aiops-alerts（一件一筆）、aiops-feedback（值班的判斷）、aiops-incidents（老師傅確認的事故）：
  每天幾件（依 dept）、有回饋的比例、誤報率、根因錯的比例、漏報、從告警開始到第一筆回饋的中位時間、
  「怎麼看出來的」有填的比例（經驗有沒有留下來）。只算、只印 Markdown；畫圖交給 Grafana。
"""
from __future__ import annotations

import pandas as pd

from sources.schema import ALERT_INDEX
from .es import ES
from . import feedback as F


def _all(es, index: str, body: dict) -> list[dict]:
    out, after = [], None
    while True:
        b = {**body, "size": 5000, "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}]}
        if after:
            b["search_after"] = after
        hits = es.req("POST", f"/{index}/_search?ignore_unavailable=true", b).get("hits", {}).get("hits", [])
        out += [{**h["_source"], "_id": h["_id"]} for h in hits]
        if len(hits) < 5000:
            return out
        after = hits[-1]["sort"]


TERMS_CHUNK = 1000   # 一次 terms 查幾個 alert_id：查詢字串別太長


def load_feedback(es, alert_ids: list[str], start, end) -> list[dict]:
    """這段的回饋＝「這段告警的回饋」（照 alert_id，不管回饋什麼時候給的：段結束後才標的也算、段內標前一段告警的不算）
    ＋「漏報」（沒有 alert_id，只能照回饋自己的時間）。"""
    ids = sorted({i for i in alert_ids if i})
    out = []
    for i in range(0, len(ids), TERMS_CHUNK):
        out += _all(es, F.FEEDBACK_INDEX, {"query": {"bool": {"filter": [{"terms": {"alert_id": ids[i:i + TERMS_CHUNK]}}]}}})
    out += _all(es, F.FEEDBACK_INDEX, {"query": {"bool": {
        "filter": [{"term": {"verdict": "missed"}}, {"range": {"@timestamp": {"gte": start.isoformat(), "lt": end.isoformat()}}}],
        "must_not": [{"exists": {"field": "alert_id"}}]}}})
    return out


def load_period(es, start, end) -> dict:
    rng = lambda f: {"range": {f: {"gte": start.isoformat(), "lt": end.isoformat()}}}
    alerts = _all(es, ALERT_INDEX, {"query": {"bool": {"filter": [rng("started_at")]}}})
    feedback = load_feedback(es, [a.get("alert_id") for a in alerts], start, end)
    incidents = _all(es, F.INCIDENT_INDEX, {"query": {"bool": {"filter": [rng("start")]}}})
    return {"alerts": alerts, "feedback": feedback, "incidents": incidents, "start": start, "end": end}


def compute(p: dict) -> dict:
    """純計算（單元測試用）。"""
    alerts = [a for a in p["alerts"] if a.get("category", "service") == "service"]
    days = max((p["end"] - p["start"]).total_seconds() / 86400, 1e-9)
    fb = pd.DataFrame(p["feedback"]) if p["feedback"] else pd.DataFrame(columns=["alert_id", "verdict", "@timestamp", "by"])
    by_alert = fb[fb.get("alert_id").notna()] if "alert_id" in fb else fb.iloc[0:0]
    # 一件事多人標：最後一筆為準
    last = by_alert.sort_values("@timestamp").drop_duplicates("alert_id", keep="last") if not by_alert.empty else by_alert
    verdicts = dict(zip(last["alert_id"], last["verdict"])) if not last.empty else {}
    reviewed = [a for a in alerts if a["alert_id"] in verdicts]
    fp = sum(verdicts[a["alert_id"]] == "false_positive" for a in reviewed)
    wrong = sum(verdicts[a["alert_id"]] == "wrong_root_cause" for a in reviewed)
    missed = int((fb["verdict"] == "missed").sum()) if not fb.empty else 0
    # 從告警開始到第一筆回饋
    ttf = []
    if not by_alert.empty:
        first = by_alert.sort_values("@timestamp").drop_duplicates("alert_id", keep="first")
        started = {a["alert_id"]: pd.Timestamp(a["started_at"]) for a in alerts}
        for r in first.to_dict("records"):
            if r["alert_id"] in started:
                ttf.append((pd.Timestamp(r["@timestamp"]) - started[r["alert_id"]]).total_seconds() / 60)
    confirmed = [i for i in p["incidents"] if i.get("status") in ("confirmed", "corrected")]
    with_how = sum(1 for i in confirmed if (i.get("how_detected") or "").strip())
    # RCA 建議採納率（POC KPI，docs/pdca/phase5-learning-loop/2026-10-feedback-nl-extract.md）：
    # 兩題（可能根因、建議的排查動作）正面 ÷ 有回答的（yes、no；not_seen 和沒填不算）。一件多筆回饋只算最後一筆
    useful = {}
    for q in ("rca_cause_useful", "rca_action_useful"):
        v = last[q].dropna() if (not last.empty and q in last) else pd.Series(dtype=object)
        useful[q] = {"yes": int((v == "yes").sum()), "answered": int(v.isin(["yes", "no"]).sum())}
    yes_n, ans_n = sum(u["yes"] for u in useful.values()), sum(u["answered"] for u in useful.values())
    # 回饋文字的抽取成功率（KPI「解析率」在正式環境的量法）：有寫一句話的回饋裡，抽成四欄（extract.ok）的比例
    with_text = last[last["feedback_text"].notna()] if (not last.empty and "feedback_text" in last) else last.iloc[0:0]
    extracted = sum(1 for x in (with_text["extract"] if "extract" in with_text else []) if isinstance(x, dict) and x.get("ok"))
    per_dept = pd.Series([a.get("dept") or "—" for a in alerts]).value_counts().to_dict() if alerts else {}
    per_svc = pd.Series([a.get("service") for a in alerts]).value_counts().head(5).to_dict() if alerts else {}
    return {"days": days, "alerts": len(alerts), "alerts_per_day": len(alerts) / days, "per_dept": per_dept, "top_services": per_svc,
            "reviewed": len(reviewed), "reviewed_rate": (len(reviewed) / len(alerts)) if alerts else None,
            "false_positive": fp, "fp_rate": (fp / len(reviewed)) if reviewed else None,
            "wrong_root_cause": wrong, "wrong_rate": (wrong / len(reviewed)) if reviewed else None, "missed": missed,
            "time_to_feedback_min": (float(pd.Series(ttf).median()) if ttf else None),
            "confirmed": len(confirmed), "how_detected_rate": (with_how / len(confirmed)) if confirmed else None,
            "reviewers": int(by_alert["by"].nunique()) if not by_alert.empty and "by" in by_alert else 0,
            "rca_useful": useful, "rca_answered": ans_n, "rca_adoption_rate": (yes_n / ans_n) if ans_n else None,
            "with_text": int(len(with_text)), "extracted": extracted, "extract_rate": (extracted / len(with_text)) if len(with_text) else None}


def _pct(x):
    return "—" if x is None else f"{x:.0%}"


def format_metrics(m: dict, label: str, m2: dict | None = None, label2: str = "") -> list[str]:
    rows = [("件數", lambda x: f"{x['alerts']}（每天 {x['alerts_per_day']:.1f}）"),
            ("有回饋的比例", lambda x: f"{_pct(x['reviewed_rate'])}（{x['reviewed']} 件，{x['reviewers']} 人）"),
            ("誤報率（回饋裡標誤報的）", lambda x: _pct(x["fp_rate"])),
            ("根因報錯的比例", lambda x: _pct(x["wrong_rate"])),
            ("漏報", lambda x: str(x["missed"])),
            ("從告警到第一筆回饋（中位）", lambda x: "—" if x["time_to_feedback_min"] is None else f"{x['time_to_feedback_min']:.0f} 分鐘"),
            ("老師傅確認的事故", lambda x: str(x["confirmed"])),
            ("「怎麼看出來的」有填的比例", lambda x: _pct(x["how_detected_rate"])),
            ("RCA 建議採納率（兩題正面 ÷ 有回答的）", lambda x: f"{_pct(x['rca_adoption_rate'])}（{x['rca_answered']} 題有回答）"),
            ("回饋一句話抽成結構化欄位的比例", lambda x: f"{_pct(x['extract_rate'])}（{x['with_text']} 件有寫）")]
    out = [f"# 量化：{label}" + (f" vs {label2}" if m2 else ""), ""]
    if m2:
        out += [f"| 指標 | {label2} | {label} |", "|---|---|---|"] + [f"| {n} | {f(m2)} | {f(m)} |" for n, f in rows]
    else:
        out += ["| 指標 | 值 |", "|---|---|"] + [f"| {n} | {f(m)} |" for n, f in rows]
    out += ["", "依部門：" + "、".join(f"{k} {v} 件" for k, v in m["per_dept"].items()) if m["per_dept"] else "依部門：—",
            "最吵的服務：" + "、".join(f"{k} {v} 件" for k, v in m["top_services"].items()) if m["top_services"] else "最吵的服務：—", ""]
    return out


def cmd_metrics(cfg: dict, args) -> int:
    es = ES()
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    start, end = pd.Timestamp(args.start, tz=tz), pd.Timestamp(args.end, tz=tz)
    m = compute(load_period(es, start, end))
    m2 = compute(load_period(es, pd.Timestamp(args.cstart, tz=tz), pd.Timestamp(args.cend, tz=tz))) if args.cstart and args.cend else None
    text = "\n".join(format_metrics(m, f"{args.start}～{args.end}", m2, f"{args.cstart}～{args.cend}" if m2 else ""))
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0

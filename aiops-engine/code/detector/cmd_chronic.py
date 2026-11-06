"""慢性病清單 chronic（detector/chronic.py、configs/baseline/chronic.yaml）。"""
from __future__ import annotations

import json

import pandas as pd

from core.anomaly import ANOMALY_INDEX
from .es import ES, doc_id
from . import chronic as C
from .common import _now, _v
from .sources import load_owners


CHRONIC_INDEX = "aiops-chronic"

CHRONIC_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "week": {"type": "keyword"}, "source": {"type": "keyword"}, "rank": {"type": "integer"},
    "keys": {"type": "flattened"}, "days": {"type": "integer"}, "total": {"type": "long"}, "per_day": {"type": "double"},
    "first_day": {"type": "date"}, "last_day": {"type": "date"}, "owner": {"type": "keyword"}, "message": {"type": "text"}}}}

def cmd_chronic(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(CHRONIC_INDEX, CHRONIC_MAPPING)
    tz = cfg.get("timezone", "Asia/Taipei")
    now = _now(args.at)
    days = cfg.get("days", 7)
    # 看最近 days 個「完整的日子」（當地時間）：今天還沒過完，不算
    end = now.tz_convert(tz).normalize().tz_convert("UTC")
    start = end - pd.Timedelta(days=days)
    week = f"{end.tz_convert(tz) - pd.Timedelta(days=1):%G-W%V}"
    owners = load_owners(cfg)
    docs, lines = [], [f"# 慢性病清單 {week}（{start.tz_convert(tz):%m/%d}～{(end - pd.Timedelta(days=1)).tz_convert(tz):%m/%d}，"
                       f"{days} 天裡至少 {cfg.get('min_days', 6)} 天有）", ""]
    for src in cfg["sources"]:
        if src["type"] == "log":
            daily = C.fetch_log_daily(es, src, start, end, tz)
            keys = src.get("keys") or [f.split(".")[-1] for f in src["by"]]
        elif src["type"] == "anomalies":
            daily = C.fetch_anomaly_daily(es, ANOMALY_INDEX, src, start, end, tz)
            keys = ["series", "target"]
        else:
            raise ValueError(f"不認得的來源種類：{src['type']}")
        found = C.find_chronic(daily, keys, min_days=src.get("min_days", cfg.get("min_days", 6)),
                               min_per_day=src.get("min_per_day", 1), top=src.get("top", cfg.get("top", 10)))
        lines += [f"## {src.get('title', src['name'])}（{len(found)} 個）", ""]
        if not found.empty:
            lines += ["| # | 對象 | 天數 | 每天約 | 共 | 負責人 |", "|---|---|---|---|---|---|"]
        for rank, r in enumerate(found.to_dict("records"), 1):
            if src["type"] == "anomalies":
                target = json.loads(r["target"])
                key = {"series": r["series"], **target}
                who = f"{r['series']}：" + "、".join(f"{v}" for v in target.values())
            else:
                key = {k: _v(r[k]) for k in keys}
                who = "、".join(str(v) if v is not None else "（無）" for v in key.values())
            owner = C.owner_of(key, owners) or "未指定"
            msg = C.describe(src, r, keys, days) if src["type"] == "log" else (
                f"{src.get('title', src['name'])}：{who}，{days} 天裡 {int(r['days'])} 天有")
            docs.append((doc_id("chronic", week, src["name"], key), {
                "@timestamp": now.isoformat(), "week": week, "source": src["name"], "rank": rank, "keys": key,
                "days": int(r["days"]), "total": int(r["total"]), "per_day": float(r["per_day"]),
                "first_day": str(r["first_day"]), "last_day": str(r["last_day"]), "owner": owner, "message": msg}))
            lines.append(f"| {rank} | {who} | {int(r['days'])} | {r['per_day']:.0f} | {int(r['total'])} | {owner} |")
        lines.append("")
    if docs:
        es.bulk_index(CHRONIC_INDEX, docs)
    print("\n".join(lines))
    return 0

"""資料品質檢查（detector/quality.py、configs/baseline/quality.yaml）。"""
from __future__ import annotations

import pandas as pd

from .es import ANOMALY_INDEX, ANOMALY_MAPPING, ES, doc_id
from . import quality as Q
from .common import RULE_FIELDS, _freq, _now, _v
from .owner import write_anomalies


def quality_hits(es, c: dict, now, window, ref_offset) -> list[dict]:
    """跑一個檢查，回傳觸發的對象（Q.eval_* 的結果）。"""
    start, rs, re_ = now - window, now - window - ref_offset, now - ref_offset
    keys, t = Q._keys(c), c["type"]
    if t == "cardinality":
        return Q.eval_cardinality(Q.fetch_cardinality(es, c, start, now, c.get("sample_values", 500)),
                                  Q.fetch_cardinality(es, c, rs, re_, 0), keys,
                                  max_values=c.get("max_values"), ratio=c.get("ratio", 3.0),
                                  min_increase=c.get("min_increase", 50), id_share_min=c.get("id_share", 0.5))
    if t == "missing":
        return Q.eval_missing(Q.fetch_missing(es, c, start, now), Q.fetch_missing(es, c, rs, re_), keys, c["fields"],
                              min_docs=c.get("min_docs", 100), max_missing=c.get("max_missing"),
                              max_increase=c.get("max_increase"))
    if t == "stopped":
        silent = pd.Timedelta(_freq(c.get("silent", "10m")))
        lookback = pd.Timedelta(_freq(c.get("lookback", "1h")))
        return Q.eval_stopped(Q.fetch_counts(es, c, now - lookback, now - silent), Q.fetch_counts(es, c, now - silent, now),
                              keys, min_docs=c.get("min_docs", 10))
    if t == "present":
        return Q.eval_present(Q.fetch_counts(es, c, start, now), keys, min_docs=c.get("min_docs", 1))
    if t == "share":
        return Q.eval_share(Q.count(es, c["part_index"], c, start, now), Q.count(es, c["index"], c, start, now),
                            Q.count(es, c["part_index"], c, rs, re_), Q.count(es, c["index"], c, rs, re_),
                            min_docs=c.get("min_docs", 100), max_share=c.get("max_share"),
                            min_increase=c.get("min_increase", 0.1))
    if t == "lag":
        return Q.eval_lag(Q.fetch_lag(es, c, start, now), keys, stat=c.get("stat", "p95"),
                          max_seconds=c.get("max_seconds", 60), min_docs=c.get("min_docs", 50),
                          absolute=c.get("absolute", False))
    raise ValueError(f"不認得的檢查種類：{t}")

def cmd_quality(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    es.req("PUT", f"/{ANOMALY_INDEX}/_mapping", {"properties": RULE_FIELDS})
    now = _now(args.at)
    slot = now.floor(_freq(cfg.get("interval", "10m")))
    ref_offset = pd.Timedelta(_freq(cfg.get("ref_offset", "24h")))
    names = set(args.only.split(",")) if args.only else None
    docs, fired, checked = [], 0, 0
    for c in cfg["checks"]:
        if names and c["name"] not in names:
            continue
        checked += 1
        window = pd.Timedelta(_freq(c.get("window", cfg.get("window", "1h"))))
        hits = quality_hits(es, c, now, window, ref_offset)
        for h in hits:
            keys = {**{k: v for k, v in h["key"].items() if _v(v) is not None},   # NaN 寫進 ES 會整批被拒
                    **({"field": h["field"]} if "field" in h else {})}
            msg = Q.describe(c, h)
            docs.append((doc_id(c["name"], keys, slot), {
                "@timestamp": slot.isoformat(), "series": c["name"], "rule": c["name"], "keys": keys,
                "value": h["value"], "median": _v(h.get("ref")), "expected_low": None, "expected_high": None, "z": None,
                "level": "quality", "direction": "up", "severity": c.get("severity", "warning"), "message": msg,
                "detected_at": pd.Timestamp.now(tz="UTC").isoformat()}))
            print(f"  [{c.get('severity', 'warning')}] {msg}")
        fired += len(hits)
    if docs:
        write_anomalies(es, docs)
    print(f"{slot.tz_convert('Asia/Taipei'):%m-%d %H:%M}：{checked} 個檢查，觸發 {fired} 個")
    return 0

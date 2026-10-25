"""規則：狀態變化、離極限多遠（detector/rules.py、configs/baseline/rules.yaml）。"""
from __future__ import annotations

import pandas as pd

from core.es import get_field
from .es import ANOMALY_INDEX, ANOMALY_MAPPING, ES, doc_id, fetch
from . import rules as R
from .common import RULE_FIELDS, _freq, _now


def cmd_rules(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    es.req("PUT", f"/{ANOMALY_INDEX}/_mapping", {"properties": RULE_FIELDS})   # 舊的 index 補上規則用的欄位
    now = _now(args.at)
    slot = now.floor(_freq(cfg.get("interval", "1m")))
    start = now - pd.Timedelta(_freq(cfg.get("lookback", "10m")))
    names = set(args.only.split(",")) if args.only else None
    docs, fired = [], 0
    for rule in cfg["rules"]:
        if names and rule["name"] not in names:
            continue
        r_start = now - pd.Timedelta(_freq(rule["lookback"])) if rule.get("lookback") else start
        if rule["type"] == "window":   # 整段算一個值（錯誤率、p95）：跟 series.yaml 同一套查法
            hits = R.evaluate_window(rule, fetch(es, rule, r_start, now, None), r_start)
        else:
            hits = R.evaluate(rule, R.fetch(es, rule, r_start, now), now)
        for h in hits:
            keys = {f: v for f, v in zip(rule["by"], h["key"])}
            # 附帶的資訊（服務名稱、節點），根因分析用來對到服務；不參與「同一個對象」的判斷
            keys.update({f: v for f in rule.get("context", []) if (v := get_field(h["last"], f)) is not None})
            msg = R.describe(rule, h)
            docs.append((doc_id(f"rule.{rule['name']}", keys, slot), {
                "@timestamp": slot.isoformat(), "series": f"rule.{rule['name']}", "rule": rule["name"], "keys": keys,
                "value": h["value"], "median": None, "expected_low": None, "expected_high": rule["threshold"],
                "z": None, "level": "rule", "direction": rule.get("op", ">="), "severity": rule.get("severity", "warning"),
                "message": msg, "since": h["since"].isoformat(), "detected_at": pd.Timestamp.now(tz="UTC").isoformat()}))
            print(f"  [{rule.get('severity', 'warning')}] {msg}")
        fired += len(hits)
    if docs:
        es.bulk_index(ANOMALY_INDEX, docs)
    print(f"{slot.tz_convert('Asia/Taipei'):%m-%d %H:%M}：{len(cfg['rules'])} 條規則，觸發 {fired} 個")
    return 0

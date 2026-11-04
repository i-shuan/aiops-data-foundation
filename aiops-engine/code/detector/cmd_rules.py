"""規則：狀態變化、離極限多遠（detector/rules.py、configs/baseline/rules.yaml）。"""
from __future__ import annotations

import pandas as pd

from core.es import get_field
from .es import ANOMALY_INDEX, ANOMALY_MAPPING, ES, doc_id, fetch
from . import rules as R
from .common import RULE_FIELDS, _freq, _now
from .owner import write_anomalies
from sources import open_backend


def approved_rules(es: ES, cfg: dict, now: pd.Timestamp | None = None) -> list[dict]:
    """老師傅核准的「升級成規則」（aiops-tuning 的 rule_suggestion，status active、expires_at 還沒到、門檻是數字）：
    跟 rules.yaml 的一起跑。now 是這一輪的時間（--at），過期用它比。"""
    from . import adjust as A
    from .tuning import load_adjustments
    try:
        adjs = load_adjustments(es, cfg)
    except Exception as e:
        print(f"  讀 aiops-tuning 的規則失敗（{e}），只跑 rules.yaml 的")
        return []
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    out = []
    for a in adjs:
        draft = a.get("rule_draft") or {}
        if a.get("kind") != "rule_suggestion" or a.get("status") != "active" or not isinstance(draft, dict) \
                or not isinstance(draft.get("threshold"), (int, float)) or not draft.get("name"):
            continue
        exp = A._ts(a.get("expires_at"))
        if exp is not None and exp <= now:
            continue
        out.append({k: v for k, v in draft.items() if not k.startswith("_")})
    return out


def cmd_rules(cfg: dict, args, backend=None) -> int:
    es = backend or open_backend(cfg, ES)
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    es.req("PUT", f"/{ANOMALY_INDEX}/_mapping", {"properties": RULE_FIELDS})   # 舊的 index 補上規則用的欄位
    now = _now(args.at)
    slot = now.floor(_freq(cfg.get("interval", "1m")))
    start = now - pd.Timedelta(_freq(cfg.get("lookback", "10m")))
    names = set(args.only.split(",")) if args.only else None
    extra = approved_rules(es, cfg, now)
    if extra:
        print(f"  從 aiops-tuning 載入 {len(extra)} 條核准的規則：{'、'.join(r['name'] for r in extra)}")
    rules = list(cfg["rules"]) + extra
    docs, fired, failed = [], 0, []
    for rule in rules:
        if names and rule["name"] not in names:
            continue
        # 一條規則壞掉（查詢錯、核准的草稿缺欄位）只跳過那一條，其他照跑、照寫
        try:
            r_start = now - pd.Timedelta(_freq(rule["lookback"])) if rule.get("lookback") else start
            if rule["type"] == "window":   # 整段算一個值（錯誤率、p95）：跟 series.yaml 同一套查法
                hits = R.evaluate_window(rule, fetch(es, rule, r_start, now, None), r_start)
            else:
                hits = R.evaluate(rule, R.fetch(es, rule, r_start, now), now)
        except Exception as e:
            failed.append(rule["name"])
            print(f"  規則 {rule['name']} 失敗，跳過：{type(e).__name__}: {str(e)[:300]}")
            continue
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
        write_anomalies(es, docs)
    print(f"{slot.tz_convert('Asia/Taipei'):%m-%d %H:%M}：{len(rules)} 條規則，觸發 {fired} 個"
          + (f"，{len(failed)} 條失敗：{'、'.join(failed)}" if failed else ""))
    return 1 if failed else 0

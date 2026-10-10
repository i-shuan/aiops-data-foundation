"""規則：狀態變化、離極限多遠。不用學、不用歷史，裝上去第一天就有用（docs/aiops-metrics.md 的做法 2、5）。

每條規則（baseline/rules.yaml）：從一個資料流讀某個欄位，依 by 切成一條條序列，看最近一段時間的每個點：

  type: value     欄位 op 門檻                    例：Pod 狀態 == Failed、節點 Ready == 0、抓取 up == 0
  type: compare   欄位 − 另一個欄位 op 門檻         例：要的副本數 − 可用副本數 >= 1（副本不夠）
  type: ratio     欄位 ÷ (分母欄位加總) op 門檻     例：記憶體 working_set ÷ (working_set + available) >= 0.9
  type: increase  累計值這段時間增加了多少 op 門檻   例：重啟次數增加 >= 1、失敗的 Job Pod 增加 >= 1

for：要「這段時間都成立」才算（例如副本不夠 3 分鐘，避免換版滾動時的短暫不足）。0 表示最新一個點成立就算。
fresh：最新的點要在這段時間內，不然當成對象已經不在了（預設 2m；increase 不看，重啟過就是重啟過）。
min_ratio：這段時間裡至少多少比例的點成立（預設 1，全部）。一直重啟的服務每次會短暫起來幾十秒，
           「全部」永遠不成立；設 0.8 表示「3 分鐘裡至少 80% 的時間不夠」。
"""
from __future__ import annotations

import operator
from collections import defaultdict

import pandas as pd

from aiops_common.es import ES, get_field, time_range

OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq, "!=": operator.ne}


def _dur(s) -> pd.Timedelta:
    return pd.Timedelta(str(s).replace("m", "min") if str(s).endswith("m") else str(s)) if s else pd.Timedelta(0)


def sample_value(rule: dict, doc: dict):
    """一個點的值（increase 以外）。欄位不存在就回 None（這個點不算）。"""
    v = get_field(doc, rule["field"])
    if v is None:
        return None
    t = rule["type"]
    if t == "value":
        return float(v)
    if t == "compare":
        other = get_field(doc, rule["other"])
        return None if other is None else float(other) - float(v)
    if t == "ratio":
        parts = [get_field(doc, f) for f in rule["den"]]
        if any(p is None for p in parts):
            return None
        den = sum(float(p) for p in parts)
        return float(v) / den if den else None
    raise ValueError(f"不認得的規則種類：{t}")


def increase(values: list[float]) -> float:
    """累計值增加了多少。數字變小代表歸零重來（例如容器被重建），從 0 開始算。"""
    total = 0.0
    for a, b in zip(values, values[1:]):
        total += b - a if b >= a else b
    return total


def evaluate(rule: dict, series: dict[tuple, list[tuple[pd.Timestamp, dict]]], now: pd.Timestamp) -> list[dict]:
    """series：{序列的 key: [(時間, 文件), …]}（時間由舊到新）。回傳觸發的：{"key", "value", "since", "last"（最新的文件）}。"""
    op, th = OPS[rule.get("op", ">=")], rule["threshold"]
    hold = _dur(rule.get("for", 0))
    out = []
    for key, pts in series.items():
        if rule["type"] == "increase":
            vals = [float(v) for _, d in pts if (v := get_field(d, rule["field"])) is not None]
            if len(vals) >= 2 and op(inc := increase(vals), th):
                out.append({"key": key, "value": inc, "since": pts[0][0], "last": pts[-1][1]})
            continue
        vals = [(t, v) for t, d in pts if (v := sample_value(rule, d)) is not None]
        # 最新的點太舊（Pod 已經刪掉、節點拿掉了）：不是「現在」的狀態，不算
        if not vals or vals[-1][0] < now - _dur(rule.get("fresh", "2m")):
            continue
        if hold == pd.Timedelta(0):
            t, v = vals[-1]
            if op(v, th):
                out.append({"key": key, "value": v, "since": t, "last": pts[-1][1]})
            continue
        recent = [(t, v) for t, v in vals if t >= now - hold]
        # 要有兩個以上的點，而且最早的點離現在至少 for 的一半（避免剛好只抓到一個點就判斷「一直都是」）
        ok = sum(op(v, th) for _, v in recent)
        if len(recent) >= 2 and recent[0][0] <= now - hold / 2 and op(recent[-1][1], th) \
                and ok >= rule.get("min_ratio", 1.0) * len(recent):
            out.append({"key": key, "value": recent[-1][1], "since": recent[0][0], "last": pts[-1][1]})
    return out


def fetch(es: ES, rule: dict, start: pd.Timestamp, end: pd.Timestamp) -> dict[tuple, list[tuple[pd.Timestamp, dict]]]:
    fields = [rule["field"], *([rule["other"]] if rule.get("other") else []), *rule.get("den", [])]
    keep = rule["by"] + rule.get("context", [])
    q = {"bool": {"filter": [time_range(start, end), {"exists": {"field": rule["field"]}}, *rule.get("filter", [])]}}
    out: dict[tuple, list] = defaultdict(list)
    after = None
    while True:
        body = {"size": 5000, "_source": ["@timestamp", *fields, *keep], "query": q,
                "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}]}
        if after:
            body["search_after"] = after
        hits = es.search(rule["index"], body)["hits"]["hits"]
        for h in hits:
            d = h["_source"]
            key = tuple(get_field(d, f) for f in rule["by"])
            ts = d["@timestamp"]
            ts = pd.Timestamp(ts, unit="ms", tz="UTC") if isinstance(ts, (int, float)) else pd.Timestamp(ts)
            out[key].append((ts, d))
        if len(hits) < 5000:
            break
        after = hits[-1]["sort"]
    return out


def describe(rule: dict, hit: dict) -> str:
    """給人看的一句話，例如「容器重啟：imc-wip-prod、crash-app-…、app（1，門檻 >= 1）」。"""
    pct = rule["type"] == "ratio"
    fmt = (lambda x: f"{x:.0%}") if pct else (lambda x: f"{x:g}")
    who = "、".join(str(k) for k in hit["key"] if k is not None)
    return f"{rule['title']}：{who}（{fmt(hit['value'])}，門檻 {rule.get('op', '>=')} {fmt(rule['threshold'])}）"

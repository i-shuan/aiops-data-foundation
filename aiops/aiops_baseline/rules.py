"""規則：狀態變化、離極限多遠。不用學、不用歷史，裝上去第一天就有用（docs/aiops-metrics.md 的做法 2、5）。

每條規則（baseline/rules.yaml）：從一個資料流讀某個欄位，依 by 切成一條條序列，看最近一段時間的每個點：

  type: value     欄位 op 門檻                    例：Pod 狀態 == Failed、節點 Ready == 0、抓取 up == 0
  type: compare   欄位 − 另一個欄位 op 門檻         例：要的副本數 − 可用副本數 >= 1（副本不夠）
  type: ratio     欄位 ÷ (分母欄位加總) op 門檻     例：記憶體 working_set ÷ (working_set + available) >= 0.9
  type: increase  累計值這段時間增加了多少 op 門檻   例：重啟次數增加 >= 1、失敗的 Job Pod 增加 >= 1
  type: increase_ratio  兩個累計值增加量的比例        例：被限流的週期 ÷ 全部週期 >= 25%（CPU throttling）
  type: rate      這段時間的加總 ÷ 秒數 op 門檻       例：GC 花的秒數 ÷ 經過的秒數 >= 10%（直方圖的 sum）
  type: total     這段時間的加總 op 門檻              例：5 分鐘內 major GC 幾次 >= 5（hist_count: true 時數直方圖的次數）

join：分母在另一種文件時（例如連線池「使用中」和「上限」分開存），用同一個對象、同一個時間點的值補進來：
  join: {field: metrics.db_client_connections_max}       之後 den 就可以寫這個欄位

for：要「這段時間都成立」才算（例如副本不夠 3 分鐘，避免換版滾動時的短暫不足）。0 表示最新一個點成立就算。
fresh：最新的點要在這段時間內，不然當成對象已經不在了（預設 2m）。
report_gone: true：對象不在了也照報（容器重啟、Job 失敗：Pod 刪掉了，重啟過就是重啟過）。
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


def hist_count(h) -> float | None:
    """ES 的 exponential_histogram 在 _source 裡的樣子：{"positive": {"counts": [...]}, "zero": {"count": n}, …}，加起來是次數。"""
    if not isinstance(h, dict):
        return None
    return float(sum((h.get("positive") or {}).get("counts", [])) + sum((h.get("negative") or {}).get("counts", []))
                 + (h.get("zero") or {}).get("count", 0))


def _window(pts, now, hold):
    return [(t, d) for t, d in pts if t >= now - hold] if hold > pd.Timedelta(0) else pts


def evaluate(rule: dict, series: dict[tuple, list[tuple[pd.Timestamp, dict]]], now: pd.Timestamp) -> list[dict]:
    """series：{序列的 key: [(時間, 文件), …]}（時間由舊到新）。回傳觸發的：{"key", "value", "since", "last"（最新的文件）}。"""
    op, th = OPS[rule.get("op", ">=")], rule["threshold"]
    hold = _dur(rule.get("for", 0))
    out = []
    for key, pts in series.items():
        if rule["type"] in ("increase_ratio", "rate", "total"):
            w = _window(pts, now, hold)
            if len(w) < 2 or w[-1][0] < now - _dur(rule.get("fresh", "2m")):
                continue
            if rule["type"] == "increase_ratio":
                num = increase([float(v) for _, d in w if (v := get_field(d, rule["field"])) is not None])
                den = increase([float(v) for _, d in w if (v := get_field(d, rule["den"][0])) is not None])
                v = num / den if den else None
            else:
                # 每一筆是那 30 秒的量（直方圖已換成差值）；同一個時間點可能有好幾筆（例如不同的 GC），全部加起來
                pick = (lambda d: hist_count(get_field(d, rule["field"]))) if rule.get("hist_count") \
                    else (lambda d: get_field(d, rule["field"]))
                total = sum(float(v) for _, d in w if (v := pick(d)) is not None)
                secs = max((w[-1][0] - w[0][0]).total_seconds() + float(rule.get("step_s", 30)), 1.0)
                v = total / secs if rule["type"] == "rate" else total
            if v is not None and op(v, th):
                out.append({"key": key, "value": v, "since": w[0][0], "last": w[-1][1]})
            continue
        if rule["type"] == "increase":
            # 對象已經不在了（Pod 刪掉）：最後幾分鐘的累計值還在回溯範圍裡，不算（除非 report_gone）
            if not rule.get("report_gone") and pts and pts[-1][0] < now - _dur(rule.get("fresh", "2m")):
                continue
            vals = [float(v) for _, d in _window(pts, now, hold) if (v := get_field(d, rule["field"])) is not None]
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
    # exists：直方圖的 sum 這種子欄位不能拿來過濾「有沒有」，要用直方圖本身（例如 metrics.jvm_gc_duration_seconds）
    out = _fetch(es, rule["index"], rule["field"], rule.get("filter", []), rule, start, end, rule.get("exists"))
    if rule.get("join"):
        # 分母在另一種文件：依「同一個對象、同一個時間點」補進來
        j = rule["join"]
        other = _fetch(es, rule["index"], j["field"], j.get("filter", []), rule, start, end)
        lookup = {(k, t): get_field(d, j["field"]) for k, pts in other.items() for t, d in pts}
        for k, pts in out.items():
            for t, d in pts:
                if (v := lookup.get((k, t))) is not None:
                    _put(d, j["field"], v)
    return out


def _put(d: dict, dotted: str, v) -> None:
    head, _, rest = dotted.partition(".")
    d.setdefault(head, {})[rest] = v   # metrics.<名稱>：名稱裡的點不拆開，跟 ES 的 _source 一樣


def _fetch(es: ES, index: str, field: str, filters: list, rule: dict, start, end, exists: str | None = None) -> dict[tuple, list]:
    fields = [field, *([rule["other"]] if rule.get("other") else []), *rule.get("den", [])]
    keep = rule["by"] + rule.get("context", [])
    q = {"bool": {"filter": [time_range(start, end), {"exists": {"field": exists or field}}, *filters]}}
    out: dict[tuple, list] = defaultdict(list)
    after = None
    while True:
        body = {"size": 5000, "_source": ["@timestamp", *fields, *keep], "query": q,
                "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}]}
        if after:
            body["search_after"] = after
        hits = es.search(index, body)["hits"]["hits"]
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
    # 比例用百分比顯示；show: number 的（例如「每核幾條執行緒」）照數字顯示。total 是次數
    pct = rule["type"] in ("ratio", "increase_ratio", "rate") and rule.get("show") != "number"
    fmt = (lambda x: f"{x:.0%}") if pct else (lambda x: f"{x:g}")
    who = "、".join(str(k) for k in hit["key"] if k is not None)
    return f"{rule['title']}：{who}（{fmt(hit['value'])}，門檻 {rule.get('op', '>=')} {fmt(rule['threshold'])}）"

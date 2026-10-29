"""baseline 用的 ES 查詢：把一條「時間序列定義」查成 DataFrame，以及寫回 baseline 和異常。連線見 core.es。"""
from __future__ import annotations

import pandas as pd

from core.es import ES, doc_id  # noqa: F401  doc_id 給 cmd_*.py 用（從 .es 一起 import）

BASELINE_INDEX = "aiops-baseline"
ANOMALY_INDEX = "aiops-anomalies"

_KEYS_MAPPING = {"type": "flattened"}
BASELINE_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "series": {"type": "keyword"}, "keys": _KEYS_MAPPING, "dow": {"type": "byte"}, "hour": {"type": "byte"},
    "level": {"type": "keyword"}, "median": {"type": "double"}, "mad": {"type": "double"},
    "p05": {"type": "double"}, "p95": {"type": "double"}, "n": {"type": "integer"},
    "trained_at": {"type": "date"}, "history_from": {"type": "date"}, "history_to": {"type": "date"},
    "resolution": {"type": "keyword"}, "method": {"type": "keyword"}, "baseline_source": {"type": "keyword"},
    "events": {"type": "double"},
}}}
ANOMALY_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "series": {"type": "keyword"}, "keys": _KEYS_MAPPING,
    "value": {"type": "double"}, "median": {"type": "double"}, "expected_low": {"type": "double"},
    "expected_high": {"type": "double"}, "z": {"type": "double"}, "level": {"type": "keyword"},
    "direction": {"type": "keyword"}, "detected_at": {"type": "date"},
    "resolution": {"type": "keyword"}, "method": {"type": "keyword"}, "baseline_source": {"type": "keyword"},
    "confidence": {"type": "keyword"}, "p": {"type": "double"},
    "status": {"type": "keyword"}, "streak": {"type": "integer"},
    "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "alert_id": {"type": "keyword"},   # 併進哪一件（investigator alert 寫回；儀表板依它撈這件的異常）
}}}


def _value_aggs(value) -> dict:
    """value 的寫法：
      count                                 每段時間的筆數
      {sum|avg|max|min: <欄位>}              該欄位的總和、平均…
      {percentile: {field: <欄位>, percent: 95}}
      {ratio: <query>}                      符合 query 的筆數 ÷ 總筆數（例如錯誤率）
    直方圖欄位（例如應用的 http.server.request.duration）：
      {hist_count: <欄位>}                   直方圖裡的次數總和（例如請求數）
      {hist_ratio: {field: <欄位>, query: <query>}}
                                            符合 query 的次數 ÷ 總次數（例如狀態碼 5xx 的比例）
      {percentile: ...} 也可以用在直方圖欄位（例如延遲 p95）
    """
    if value == "count":
        return {}
    (kind, arg), = value.items()
    if kind in ("sum", "avg", "max", "min"):
        return {"v": {kind: {"field": arg}}}
    if kind == "hist_count":
        return {"v": {"value_count": {"field": arg}}}
    if kind == "hist_ratio":
        n = {"value_count": {"field": arg["field"]}}
        return {"v": n, "num": {"filter": arg["query"], "aggs": {"v": n}}}
    if kind == "percentile":
        return {"v": {"percentiles": {"field": arg["field"], "percents": [arg["percent"]]}},
                "n": {"value_count": {"field": arg["field"]}}}
    if kind == "ratio":
        return {"num": {"filter": arg}}
    raise ValueError(f"不認得的 value：{value}")


def _bucket_value(value, b: dict):
    if value == "count":
        return float(b["doc_count"])
    (kind, _), = value.items()
    if kind == "ratio":
        return b["num"]["doc_count"] / b["doc_count"] if b["doc_count"] else None
    if kind == "hist_ratio":
        total = b["v"]["value"]
        return b["num"]["v"]["value"] / total if total else None
    if kind == "percentile":
        vals = b["v"]["values"]
        return next(iter(vals.values()))
    return b["v"]["value"]


def value_kind(spec: dict) -> str:
    """count：值本身就是事件數（筆數、請求數）；rate：比例或百分位，事件數是底下的請求數；
    gauge：量測值（CPU、記憶體），不看事件數。預先統計好的筆數（例如 {sum: requests}）設 events: value。"""
    if spec.get("events") == "value":
        return "count"
    if isinstance(spec.get("events"), dict):   # 預先統計好的資料：事件數在另一個欄位（例如 {sum: requests}）
        return spec.get("kind", "rate")
    value = spec["value"]
    if value == "count":
        return "count"
    (kind, _), = value.items()
    return {"hist_count": "count", "ratio": "rate", "hist_ratio": "rate", "percentile": "rate"}.get(kind, "gauge")


def _bucket_events(value, b: dict):
    """這個時間窗裡底下的事件數：選粒度用（detector.model.pick_resolution）。量測值是 None。"""
    if value == "count":
        return float(b["doc_count"])
    (kind, _), = value.items()
    if kind in ("hist_count", "hist_ratio"):
        return b["v"]["value"]
    if kind == "ratio":
        return float(b["doc_count"])
    if kind == "percentile":
        return b["n"]["value"]
    return None


def fetch(es: ES, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str | None) -> pd.DataFrame:
    """有 keys 時（統一的名字，例如 service、method、route），回傳的欄位用 keys 取代 by 的欄位路徑。
    同一個指標不同語言名字不同的問題在 node Collector 改名解決（docs/pdca/phase1-collect/2026-10-field-unify.md），這裡不再分語言各查一次。"""
    df = _fetch_one(es, spec, start, end, interval)
    if spec.get("keys"):
        df = df.rename(columns=dict(zip(spec["by"], spec["keys"])))
    return df


def _fetch_one(es: ES, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str | None) -> pd.DataFrame:
    """把一條序列定義在 [start, end) 的資料，依 by 欄位和 interval 切好。interval 是 None 時整段算一個值（ts 是 end），
    跟兄弟比（peer.py）用：每個 Pod 最近 10 分鐘的錯誤率、p95。

    回傳欄位：by 的每個欄位 + ts + value + n（底下的事件數，見 _bucket_events；events: value 時就是 value）。筆數類（count、或 fill_zero: true）沒有資料的時段補 0：
    沒有請求就是 0，不是「不知道」。這也是為什麼資料不能缺，缺了會被學成「平常會掉到 0」。
    """
    by = spec["by"]
    filters = [{"range": {spec.get("time_field", "@timestamp"): {"gte": start.isoformat(), "lt": end.isoformat()}}}]
    if spec.get("filter"):
        filters.append(spec["filter"])
    sources = [{f"k{i}": {"terms": {"field": f}}} for i, f in enumerate(by)]
    if interval:
        sources.append({"ts": {"date_histogram": {"field": spec.get("time_field", "@timestamp"), "fixed_interval": interval}}})
    aggs = _value_aggs(spec["value"])
    if isinstance(spec.get("events"), dict):
        aggs["n"] = _value_aggs(spec["events"])["v"]
    body = {"size": 0, "query": {"bool": {"filter": filters}},
            "aggs": {"s": {"composite": {"size": 5000, "sources": sources}, "aggs": aggs}}}
    rows, after = [], None
    while True:
        if after:
            body["aggs"]["s"]["composite"]["after"] = after
        res = es.req("POST", f"/{spec['index']}/_search?ignore_unavailable=true&allow_no_indices=true", body)
        agg = res.get("aggregations", {}).get("s", {})
        for b in agg.get("buckets", []):
            row = {f: b["key"][f"k{i}"] for i, f in enumerate(by)}
            row["ts"] = pd.Timestamp(b["key"]["ts"], unit="ms", tz="UTC") if interval else end
            row["value"] = _bucket_value(spec["value"], b)
            ev = spec.get("events")
            row["n"] = row["value"] if ev == "value" else b["n"]["value"] if isinstance(ev, dict) else _bucket_events(spec["value"], b)
            rows.append(row)
        after = agg.get("after_key")
        if not after:
            break
    df = pd.DataFrame(rows, columns=by + ["ts", "value", "n"])
    if interval and (spec["value"] == "count" or spec.get("fill_zero")) and not df.empty:
        # 有 filter 的序列（例如只數 error log）不能拿自己的資料判斷收集系統有沒有在跑：沒有任何服務出錯的時段
        # 會被當成「不知道」，稀少的錯誤數就只剩有錯的時段可學，平常被學高
        alive = alive_times(es, spec, start, end, interval) if spec.get("filter") else None
        df = _fill_zero(df, by, start, end, interval, alive)
    return df


def alive_times(es: ES, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str) -> pd.DatetimeIndex:
    """收集系統有在送資料的時間窗：這條序列的 index、不加序列自己的 filter，任何一筆資料就算。
    整個 index 都沒有資料的時段＝收集系統停了（「不知道」），不是零流量。"""
    tf = spec.get("time_field", "@timestamp")
    body = {"size": 0, "query": {"bool": {"filter": [{"range": {tf: {"gte": start.isoformat(), "lt": end.isoformat()}}}]}},
            "aggs": {"t": {"composite": {"size": 10000, "sources": [{"ts": {"date_histogram": {"field": tf, "fixed_interval": interval}}}]}}}}
    out, after = [], None
    while True:
        if after:
            body["aggs"]["t"]["composite"]["after"] = after
        agg = es.req("POST", f"/{spec['index']}/_search?ignore_unavailable=true&allow_no_indices=true", body
                     ).get("aggregations", {}).get("t", {})
        out += [b["key"]["ts"] for b in agg.get("buckets", [])]
        after = agg.get("after_key")
        if not after:
            break
    return pd.DatetimeIndex(pd.to_datetime(out, unit="ms", utc=True))


def _fill_zero(df: pd.DataFrame, by: list[str], start, end, interval, alive=None) -> pd.DataFrame:
    """筆數類的序列：沒資料的時段補 0。但有兩種情況不補，當成「不知道」（NaN）：
    1. 這條序列第一次出現之前：服務還沒上線，不是「平常是 0」。
    2. 收集系統本身停了（ES、Collector 停機）的時段，不是大家都沒流量。alive 是收集系統有在送資料的時段
       （alive_times：整個 index、不加 filter）；沒給就用這次查到的資料（沒有 filter 的序列兩者一樣）。
    """
    if df.empty:
        return df
    freq = interval[:-1] + "min" if interval.endswith("m") else interval   # ES 的 5m = pandas 的 5min
    grid = pd.date_range(start.floor(freq), end, freq=freq, inclusive="left")
    alive = pd.Index(df["ts"].unique()) if alive is None else pd.Index(alive).union(pd.Index(df["ts"].unique()))
    out = []
    for key_vals, g in df.groupby(by, sort=False):
        key_vals = key_vals if isinstance(key_vals, tuple) else (key_vals,)
        s = g.set_index("ts")["value"].reindex(grid)
        n = g.set_index("ts")["n"].reindex(grid) if "n" in g else pd.Series(None, index=grid, dtype=float)
        known = (grid >= g["ts"].min()) & grid.isin(alive)
        fill = known & s.isna()
        s[fill] = 0.0
        n[fill] = 0.0
        part = pd.DataFrame({"ts": s.index, "value": s.to_numpy(), "n": n.to_numpy()})
        for f, v in zip(by, key_vals):
            part[f] = v
        out.append(part)
    return pd.concat(out, ignore_index=True)[by + ["ts", "value", "n"]]


# 讀回來的欄位。method 存成 baseline_method：序列的 keys 也可能有 method（HTTP method），會撞名
BASELINE_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution", "baseline_method", "baseline_source",
                    "trained_at")
_RENAMED = {"baseline_method": "method"}


def load_baseline(es: ES, series: str, by: list[str]) -> pd.DataFrame:
    """這條序列自己學出來的 baseline（不含借給別人用的組 baseline）。"""
    rows, after = [], None
    while True:
        body = {"size": 10000, "query": {"bool": {"filter": [{"term": {"series": series}}],
                                                  "must_not": [{"prefix": {"baseline_source": "group:"}}]}},
                "sort": [{"_doc": "asc"}]}
        if after:
            body["search_after"] = after
        hits = es.req("POST", f"/{BASELINE_INDEX}/_search?ignore_unavailable=true", body).get("hits", {}).get("hits", [])
        for h in hits:
            s = h["_source"]
            rows.append({**{f: s["keys"].get(f) for f in by}, **{k: s.get(_RENAMED.get(k, k)) for k in BASELINE_COLUMNS}})
        if len(hits) < 10000:
            break
        after = hits[-1]["sort"]
    return pd.DataFrame(rows, columns=by + list(BASELINE_COLUMNS))


def load_group_baselines(es: ES, series: str) -> pd.DataFrame:
    """train 時從成員算出來的組 baseline：group + dow、hour、level、median、mad、resolution。"""
    body = {"size": 10000, "query": {"bool": {"filter": [{"term": {"series": series}},
                                                         {"prefix": {"baseline_source": "group:"}}]}}}
    hits = es.req("POST", f"/{BASELINE_INDEX}/_search?ignore_unavailable=true", body).get("hits", {}).get("hits", [])
    rows = [{"group": h["_source"]["baseline_source"][len("group:"):],
             **{k: h["_source"].get(k) for k in ("dow", "hour", "level", "median", "mad", "resolution")}} for h in hits]
    return pd.DataFrame(rows, columns=["group", "dow", "hour", "level", "median", "mad", "resolution"])

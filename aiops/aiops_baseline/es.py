"""baseline 用的 ES 查詢：把一條「時間序列定義」查成 DataFrame，以及寫回 baseline 和異常。連線見 aiops_common.es。"""
from __future__ import annotations

import pandas as pd

from aiops_common.es import ES, doc_id  # noqa: F401（doc_id 給 __main__ 用）

BASELINE_INDEX = "aiops-baseline"
ANOMALY_INDEX = "aiops-anomalies"

_KEYS_MAPPING = {"type": "flattened"}
BASELINE_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "series": {"type": "keyword"}, "keys": _KEYS_MAPPING, "dow": {"type": "byte"}, "hour": {"type": "byte"},
    "level": {"type": "keyword"}, "median": {"type": "double"}, "mad": {"type": "double"},
    "p05": {"type": "double"}, "p95": {"type": "double"}, "n": {"type": "integer"},
    "trained_at": {"type": "date"}, "history_from": {"type": "date"}, "history_to": {"type": "date"},
}}}
ANOMALY_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "series": {"type": "keyword"}, "keys": _KEYS_MAPPING,
    "value": {"type": "double"}, "median": {"type": "double"}, "expected_low": {"type": "double"},
    "expected_high": {"type": "double"}, "z": {"type": "double"}, "level": {"type": "keyword"},
    "direction": {"type": "keyword"}, "detected_at": {"type": "date"},
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
        return {"v": {"percentiles": {"field": arg["field"], "percents": [arg["percent"]]}}}
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


def fetch(es: ES, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str) -> pd.DataFrame:
    """有 variants 時：同一個指標在不同語言的欄位名稱不同（Java 經 Prometheus 是 http_server_request_duration_seconds、
    .NET 用 OTLP 推是 http.server.request.duration），每個 variant 各查一次，by 的欄位換成統一的 keys 再合併。"""
    if spec.get("variants"):
        parts = []
        for v in spec["variants"]:
            sub = {k: x for k, x in spec.items() if k not in ("variants", "keys")} | v
            df = _fetch_one(es, sub, start, end, interval)
            parts.append(df.rename(columns=dict(zip(v["by"], spec["keys"]))))
        parts = [p for p in parts if not p.empty]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=spec["keys"] + ["ts", "value"])
    return _fetch_one(es, spec, start, end, interval)


def _fetch_one(es: ES, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str) -> pd.DataFrame:
    """把一條序列定義在 [start, end) 的資料，依 by 欄位和 interval 切好。

    回傳欄位：by 的每個欄位 + ts + value。筆數類（count、或 fill_zero: true）沒有資料的時段補 0：
    沒有請求就是 0，不是「不知道」。這也是為什麼資料不能缺，缺了會被學成「平常會掉到 0」。
    """
    by = spec["by"]
    filters = [{"range": {spec.get("time_field", "@timestamp"): {"gte": start.isoformat(), "lt": end.isoformat()}}}]
    if spec.get("filter"):
        filters.append(spec["filter"])
    sources = [{f"k{i}": {"terms": {"field": f}}} for i, f in enumerate(by)]
    sources.append({"ts": {"date_histogram": {"field": spec.get("time_field", "@timestamp"), "fixed_interval": interval}}})
    body = {"size": 0, "query": {"bool": {"filter": filters}},
            "aggs": {"s": {"composite": {"size": 5000, "sources": sources}, "aggs": _value_aggs(spec["value"])}}}
    rows, after = [], None
    while True:
        if after:
            body["aggs"]["s"]["composite"]["after"] = after
        res = es.req("POST", f"/{spec['index']}/_search?ignore_unavailable=true&allow_no_indices=true", body)
        agg = res.get("aggregations", {}).get("s", {})
        for b in agg.get("buckets", []):
            row = {f: b["key"][f"k{i}"] for i, f in enumerate(by)}
            row["ts"] = pd.Timestamp(b["key"]["ts"], unit="ms", tz="UTC")
            row["value"] = _bucket_value(spec["value"], b)
            rows.append(row)
        after = agg.get("after_key")
        if not after:
            break
    df = pd.DataFrame(rows, columns=by + ["ts", "value"])
    if spec["value"] == "count" or spec.get("fill_zero"):
        df = _fill_zero(df, by, start, end, interval)
    return df


def _fill_zero(df: pd.DataFrame, by: list[str], start, end, interval) -> pd.DataFrame:
    """筆數類的序列：沒資料的時段補 0。但有兩種情況不補，當成「不知道」（NaN）：
    1. 這條序列第一次出現之前：服務還沒上線，不是「平常是 0」。
    2. 整個查詢範圍裡，所有序列都沒有資料的時段：那是收集系統本身停了（ES、Collector 停機），不是大家都沒流量。
    """
    if df.empty:
        return df
    freq = interval[:-1] + "min" if interval.endswith("m") else interval   # ES 的 5m = pandas 的 5min
    grid = pd.date_range(start.floor(freq), end, freq=freq, inclusive="left")
    alive = pd.Index(df["ts"].unique())          # 至少有一條序列有資料的時段
    out = []
    for key_vals, g in df.groupby(by, sort=False):
        key_vals = key_vals if isinstance(key_vals, tuple) else (key_vals,)
        s = g.set_index("ts")["value"].reindex(grid)
        known = (grid >= g["ts"].min()) & grid.isin(alive)
        s[known & s.isna()] = 0.0
        part = pd.DataFrame({"ts": s.index, "value": s.to_numpy()})
        for f, v in zip(by, key_vals):
            part[f] = v
        out.append(part)
    return pd.concat(out, ignore_index=True)[by + ["ts", "value"]]


def load_baseline(es: ES, series: str, by: list[str]) -> pd.DataFrame:
    rows, after = [], None
    while True:
        body = {"size": 10000, "query": {"term": {"series": series}}, "sort": [{"_doc": "asc"}]}
        if after:
            body["search_after"] = after
        hits = es.req("POST", f"/{BASELINE_INDEX}/_search?ignore_unavailable=true", body).get("hits", {}).get("hits", [])
        for h in hits:
            s = h["_source"]
            rows.append({**{f: s["keys"].get(f) for f in by}, **{k: s[k] for k in ("dow", "hour", "level", "median", "mad")}})
        if len(hits) < 10000:
            break
        after = hits[-1]["sort"]
    return pd.DataFrame(rows, columns=by + ["dow", "hour", "level", "median", "mad"])

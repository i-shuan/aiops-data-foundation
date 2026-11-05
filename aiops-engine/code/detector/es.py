"""baseline 的序列查詢與 aiops-baseline、aiops-anomalies 的 mapping。連線見 core.es。
查詢本身在 sources/es_source.py（ESSourceBackend.series、alive_times）、sources/stores.py（ESStore.load_baseline…）；
這裡的 fetch／alive_times／load_baseline／load_group_baselines 只是 as_backend(es).<方法>，名字留給指令與測試。
value 的寫法 → ES aggregation（_value_aggs、_bucket_value、_bucket_events）留在這裡：sources/shape.py 的 pandas 版跟它是同一套定義。"""
from __future__ import annotations

import pandas as pd

from core.es import ES, doc_id  # noqa: F401  doc_id 給 cmd_*.py 用（從 .es 一起 import）
from sources.base import as_backend
from sources.shape import fill_zero as _fill_zero  # noqa: F401  補 0 的邏輯在 sources/shape.py（ES 與其他來源共用）；名字留給既有的 import
from sources.stores import BASELINE_COLUMNS, _RENAMED  # noqa: F401  baseline 讀回來的欄位，一份定義在 sources/stores.py

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
    # 哪一版 engine 產生的（core/version.py；owner.write_anomalies 補）。放在這裡而不是 RULE_FIELDS：index 是 dynamic false，
    # 但 ensure_index 對既有 index 也會 PUT _mapping 把這份 properties 補上，detect、rules、peer、quality、trace 每輪都呼叫，不用另外補
    "engine_version": {"type": "keyword"},
}}}


def _value_aggs(value, scale: float = 1.0) -> dict:
    """value 的寫法：
      count                                 每段時間的筆數
      {sum|avg|max|min: <欄位>}              該欄位的總和、平均…
      {percentile: {field: <欄位>, percent: 95}}
      {ratio: <query>}                      符合 query 的筆數 ÷ 總筆數（例如錯誤率）
      {div: {num: <欄位>|{field, query}, den: <欄位>}}
                                            兩個欄位的總和相除（例如 APM 預先統計的往外呼叫：耗時總和 ÷ 次數＝平均耗時；
                                            num 加 query 就是「符合的次數 ÷ 全部次數」）；事件數是 den 的總和
    直方圖欄位（例如應用的 http.server.request.duration）：
      {hist_count: <欄位>}                   直方圖裡的次數總和（例如請求數）
      {hist_ratio: {field: <欄位>, query: <query>}}
                                            符合 query 的次數 ÷ 總次數（例如狀態碼 5xx 的比例）
      {hist_ratio_above: {field: <欄位>, gt: <值>}}
                                            直方圖裡超過 gt 的次數比例（例如超過 2 秒的請求比例）：ES 的 percentile_ranks
                                            給「≤ gt 的百分比」，1 − 它就是；值在桶之間時 ES 會內插
      {percentile: ...} 也可以用在直方圖欄位（例如延遲 p95）
    scale：序列的 `scale`，資料的單位 × scale ＝ 序列的單位（APM 存微秒、序列要秒就是 1e-6）。有單位的值（percentile、
      sum／avg／max／min、div）乘上去，門檻（hist_ratio_above 的 gt）除回去；比例、次數沒有單位，不動。
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
    if kind == "hist_ratio_above":
        return {"v": {"percentile_ranks": {"field": arg["field"], "values": [arg["gt"] / scale]}},
                "n": {"value_count": {"field": arg["field"]}}}
    if kind == "ratio":
        return {"num": {"filter": arg}}
    if kind == "div":
        num = arg["num"] if isinstance(arg["num"], dict) else {"field": arg["num"]}
        s = {"sum": {"field": num["field"]}}
        return {"den": {"sum": {"field": arg["den"]}},
                "num": {"filter": num["query"], "aggs": {"v": s}} if num.get("query") else s}
    raise ValueError(f"不認得的 value：{value}")


def _bucket_value(value, b: dict, scale: float = 1.0):
    if value == "count":
        return float(b["doc_count"])
    (kind, _), = value.items()
    if kind == "ratio":
        return b["num"]["doc_count"] / b["doc_count"] if b["doc_count"] else None
    if kind == "hist_count":     # 次數，沒有單位
        return b["v"]["value"]
    if kind == "hist_ratio":
        total = b["v"]["value"]
        return b["num"]["v"]["value"] / total if total else None
    if kind == "percentile":
        vals = b["v"]["values"]
        v = next(iter(vals.values()))
        return None if v is None else v * scale
    if kind == "hist_ratio_above":
        if not b["n"]["value"]:
            return None
        rank = next(iter(b["v"]["values"].values()))      # ≤ gt 的百分比（0～100）
        return None if rank is None else max(0.0, 1.0 - rank / 100.0)
    if kind == "div":
        den = b["den"]["value"]
        if not den:
            return None
        num = b["num"]["v"]["value"] if "v" in b["num"] else b["num"]["value"]
        return (num or 0) / den * scale
    v = b["v"]["value"]
    return None if v is None else v * scale


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
    return {"hist_count": "count", "ratio": "rate", "hist_ratio": "rate", "hist_ratio_above": "rate", "percentile": "rate",
            "div": "rate"}.get(kind, "gauge")


def _bucket_events(value, b: dict):
    """這個時間窗裡底下的事件數：選粒度用（detector.model.pick_resolution）。量測值是 None。"""
    if value == "count":
        return float(b["doc_count"])
    (kind, _), = value.items()
    if kind in ("hist_count", "hist_ratio"):
        return b["v"]["value"]
    if kind == "ratio":
        return float(b["doc_count"])
    if kind in ("percentile", "hist_ratio_above"):
        return b["n"]["value"]
    if kind == "div":
        return b["den"]["value"]
    return None


def fetch(es, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str | None) -> pd.DataFrame:
    """一條序列定義 → keys（沒有就 by）+ ts + value + n。es 是 ES 就在 ES 端 aggregation（sources/es_source.py），
    是檔案、HTTP、記憶體的 Backend 就在 pandas 整形（sources/shape.py），回傳格式一樣。"""
    return as_backend(es).series(spec, start, end, interval)


def alive_times(es, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str) -> pd.DatetimeIndex:
    """收集系統有在送資料的時間窗（Backend.alive_times）。"""
    return as_backend(es).alive_times(spec, start, end, interval)


def load_baseline(es, series: str, by: list[str]) -> pd.DataFrame:
    """這條序列自己學出來的 baseline（不含借給別人用的組 baseline）：欄位 by + BASELINE_COLUMNS。"""
    return as_backend(es).load_baseline(series, by)


def load_group_baselines(es, series: str) -> pd.DataFrame:
    """train 時從成員算出來的組 baseline：group + dow、hour、level、median、mad、resolution。"""
    return as_backend(es).load_group_baselines(series)

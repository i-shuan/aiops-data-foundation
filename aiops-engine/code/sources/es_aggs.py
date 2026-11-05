"""序列 value 的寫法 → ES aggregation、從 bucket 取值（ESSourceBackend.series 用）。
shape.py 的 pandas 版跟這裡是同一套定義；value_kind 是 detector 選粒度、看事件數時用的分類。"""
from __future__ import annotations


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

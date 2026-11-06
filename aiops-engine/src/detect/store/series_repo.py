"""原始資料（metrics、log、trace 的索引，由序列、規則的設定給）→ 模型的標準輸入，aggregation 在 ES 端算：
  series   keys… + ts + value + n        一條序列在一個時間窗的值；n 是底下的事件數（動態基準線、window 型規則、同儕比對用 interval=None）
  points   {key 元組: [(ts, doc), …]}    一個對象一個時間點的原始文件（value／compare／ratio／increase／slope 型規則）
  rows     [{欄位: 值}, …]               來源的原始列（ingest 讀 ML／FDC 判好的異常、工廠 paths-merge 讀 MES）
"""
from __future__ import annotations

from collections import defaultdict

import pandas as pd

from shared.connection import get_field, time_range
from shared.repository import Repository

COLUMNS = ["ts", "value", "n"]


def to_freq(interval: str) -> str:
    return interval[:-1] + "min" if interval.endswith("m") else interval


# ── 序列的 value 寫法 → ES aggregation；回來的 bucket → 值、事件數 ──

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


def _bucket_events(value, b: dict):
    """這個時間窗裡底下的事件數：選粒度用（detect/strategy/dynamic_baseline.pick_resolution）。量測值是 None。"""
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


def _put(d: dict, dotted: str, v) -> None:
    head, _, rest = dotted.partition(".")
    d.setdefault(head, {})[rest] = v   # metrics.<名稱>：名稱裡的點不拆開，跟 ES 的 _source 一樣


def fill_zero(df: pd.DataFrame, by: list[str], start, end, interval, alive=None) -> pd.DataFrame:
    """筆數類的序列：沒資料的時段補 0。但兩種情況當成「不知道」（NaN）：
    1. 這條序列第一次出現之前：服務還沒上線，不是「平常是 0」。
    2. 收集系統本身停了的時段：alive 是有資料的時段（整個 index、不加 filter）；沒給就用這次查到的資料。"""
    if df.empty:
        return df
    freq = to_freq(interval)
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
    return pd.concat(out, ignore_index=True)[by + COLUMNS]


class SeriesRepository(Repository):
    # ── series（動態基準線、同儕比對、window 型規則）──
    def fetch(self, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str | None) -> pd.DataFrame:
        """一條序列定義（series.yaml 的一筆：index、by、filter、value、keys、scale、events、fill_zero）在 [start, end) 的資料，
        依 by 欄位和 interval 切好（composite aggregation 翻頁）。interval 是 None 時整段算一個值（ts 是 end），同儕比對用。

        回傳欄位：by 的每個欄位（有 keys 時改成 keys 的名字）+ ts + value + n（底下的事件數；events: value 時就是 value）。
        筆數類（count、或 fill_zero: true）沒有資料的時段補 0：沒有請求就是 0，不是「不知道」。這也是為什麼資料不能缺，缺了會被學成「平常會掉到 0」。"""
        by = spec["by"]
        tf = spec.get("time_field", "@timestamp")
        filters = [{"range": {tf: {"gte": start.isoformat(), "lt": end.isoformat()}}}]
        if spec.get("filter"):
            filters.append(spec["filter"])
        sources = [{f"k{i}": {"terms": {"field": f}}} for i, f in enumerate(by)]
        if interval:
            sources.append({"ts": {"date_histogram": {"field": tf, "fixed_interval": interval}}})
        scale = float(spec.get("scale") or 1.0)      # 資料的單位換成序列的單位（APM 的微秒 → 秒），見 _value_aggs
        aggs = _value_aggs(spec["value"], scale)
        if isinstance(spec.get("events"), dict):
            aggs["n"] = _value_aggs(spec["events"])["v"]
        body = {"size": 0, "query": {"bool": {"filter": filters}}, "aggs": {"s": {"composite": {"size": 5000, "sources": sources}, "aggs": aggs}}}
        rows = []
        for b in self._composite(body, ("s",), spec["index"]):
            row = {f: b["key"][f"k{i}"] for i, f in enumerate(by)}
            row["ts"] = pd.Timestamp(b["key"]["ts"], unit="ms", tz="UTC") if interval else end
            row["value"] = _bucket_value(spec["value"], b, scale)
            ev = spec.get("events")
            row["n"] = row["value"] if ev == "value" else b["n"]["value"] if isinstance(ev, dict) else _bucket_events(spec["value"], b)
            rows.append(row)
        df = pd.DataFrame(rows, columns=by + COLUMNS)
        if interval and (spec["value"] == "count" or spec.get("fill_zero")) and not df.empty:
            # 有 filter 的序列（例如只數 error log）不能拿自己的資料判斷收集系統有沒有在跑：沒有任何服務出錯的時段
            # 會被當成「不知道」，稀少的錯誤數就只剩有錯的時段可學，平常被學高
            alive = self.alive_times(spec, start, end, interval) if spec.get("filter") else None
            df = fill_zero(df, by, start, end, interval, alive)
        if spec.get("keys"):
            df = df.rename(columns=dict(zip(by, spec["keys"])))
        return df

    def alive_times(self, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str) -> pd.DatetimeIndex:
        """收集系統有在送資料的時間窗：這條序列的 index、不加序列自己的 filter，任何一筆資料就算。
        整個 index 都沒有資料的時段＝收集系統停了（「不知道」），不是零流量。"""
        tf = spec.get("time_field", "@timestamp")
        body = {"size": 0, "query": {"bool": {"filter": [{"range": {tf: {"gte": start.isoformat(), "lt": end.isoformat()}}}]}},
                "aggs": {"t": {"composite": {"size": 10000, "sources": [{"ts": {"date_histogram": {"field": tf, "fixed_interval": interval}}}]}}}}
        out = [b["key"]["ts"] for b in self._composite(body, ("t",), spec["index"])]
        return pd.DatetimeIndex(pd.to_datetime(out, unit="ms", utc=True))

    # ── points（靜態臨界值規則）──
    def points(self, rule: dict, start: pd.Timestamp, end: pd.Timestamp) -> dict[tuple, list[tuple[pd.Timestamp, dict]]]:
        """{序列的 key: [(時間, 文件), …]}。join：分母在另一種文件，依「同一個對象、同一個時間點」補進來。"""
        # exists：直方圖的 sum 這種子欄位不能拿來過濾「有沒有」，要用直方圖本身（例如 metrics.jvm_gc_duration_seconds）
        out = self._points(rule["index"], rule["field"], rule.get("filter", []), rule, start, end, rule.get("exists"))
        if rule.get("join"):
            j = rule["join"]
            other = self._points(rule["index"], j["field"], j.get("filter", []), rule, start, end)
            lookup = {(k, t): get_field(d, j["field"]) for k, pts in other.items() for t, d in pts}
            for k, pts in out.items():
                for t, d in pts:
                    if (v := lookup.get((k, t))) is not None:
                        _put(d, j["field"], v)
        return out

    def _points(self, index: str, field: str, filters: list, rule: dict, start, end, exists: str | None = None) -> dict[tuple, list]:
        fields = [field, *([rule["other"]] if rule.get("other") else []), *rule.get("den", [])]
        keep = rule["by"] + rule.get("context", [])
        q = {"bool": {"filter": [time_range(start, end), {"exists": {"field": exists or field}}, *filters]}}
        out: dict[tuple, list] = defaultdict(list)
        for h in self._scan({"_source": ["@timestamp", *fields, *keep], "query": q}, index):
            d = h["_source"]
            key = tuple(get_field(d, f) for f in rule["by"])
            ts = d["@timestamp"]
            ts = pd.Timestamp(ts, unit="ms", tz="UTC") if isinstance(ts, (int, float)) else pd.Timestamp(ts)
            out[key].append((ts, d))
        return out

    # ── rows（ingest、paths-merge）──
    def rows(self, inp: dict, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
        """inp（index、time_field、filter）在 [start, end) 的原始列；每列多帶 _source_doc_id（ES 的 _id）：ingest 算異常 _id 用。"""
        tf = inp.get("time_field", "@timestamp")
        filters = [{"range": {tf: {"gte": start.isoformat(), "lt": end.isoformat()}}}]
        if inp.get("filter"):
            filters.append(inp["filter"])
        return [{**h["_source"], "_source_doc_id": h["_id"]}
                for h in self._scan({"query": {"bool": {"filter": filters}}}, inp["index"], sort=[{tf: "asc"}, {"_doc": "asc"}])]

    def rows_where(self, inp: dict, field: str, values: list[str]) -> list[dict]:
        """inp 的索引裡 field 在 values 的全部列（不限時間；工廠的履歷要一個 lot 全部的站），依 time_field 排。"""
        tf = inp.get("time_field", "@timestamp")
        out, vals = [], sorted(values)
        for i in range(0, len(vals), 1000):
            body = {"query": {"bool": {"filter": [{"terms": {field: vals[i:i + 1000]}}]}}}
            out += [h["_source"] for h in self._scan(body, inp["index"], size=10000, sort=[{tf: "asc"}, {"_doc": "asc"}])]
        return out

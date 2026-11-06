"""原始資料在 ES 的 Backend：ESSourceBackend（讀 ES，結果寫別的 Store：離線實驗）、ESBackend（讀寫都是 ES：設定沒寫 source 的預設）。
查詢就是原本 detector/es.py、detector/rules.py、core/traces.py、detector/cmd_ingest.py 裡打 ES 的那幾段，搬來當方法；aggregation 在 ES 端算。
es 是 core.es.ES，或測試裡只有 req／search 的假 ES（base.as_backend 包進來）。"""
from __future__ import annotations

from collections import defaultdict

import pandas as pd

from aiops.core.connection import get_field, time_range
from aiops.core.entity.trace import _APM_FIELDS, _SPAN_FIELDS, from_merged, schema_of, to_span
from aiops.core.repository.base import Backend, Store
from aiops.core.repository.shape import fill_zero
from aiops.core.repository.stores import ESStore

_SEARCH = "_search?ignore_unavailable=true&allow_no_indices=true"


# ── 序列的 value 寫法 → ES aggregation；回來的 bucket → 值、事件數（sources/shape.py 的 pandas 版跟它是同一套定義）──

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


def _put(d: dict, dotted: str, v) -> None:
    head, _, rest = dotted.partition(".")
    d.setdefault(head, {})[rest] = v   # metrics.<名稱>：名稱裡的點不拆開，跟 ES 的 _source 一樣


class ESSourceBackend(Backend):
    """原始資料在 ES、結果寫 store（file／memory：例如離線實驗；ESBackend 給 ESStore 就是全 ES）。"""
    kind = "es"

    def __init__(self, es, store: Store):
        self.es, self.store = es, store

    def raw_search(self, index, body):
        return self.es.search(index, body)

    def raw_req(self, method, path, body=None, ok=None):
        return self.es.req(method, path, body, **({"ok": ok} if ok else {}))

    # ── series（M1、M2、M3 window）──
    def series(self, spec, start, end, interval):
        """把一條序列定義在 [start, end) 的資料，依 by 欄位和 interval 切好（composite aggregation 翻頁）。interval 是 None 時整段算一個值
        （ts 是 end），Pod 同儕橫向比對（peer.py）用：每個 Pod 最近 10 分鐘的錯誤率、p95。

        回傳欄位：by 的每個欄位（有 keys 時改成 keys 的名字）+ ts + value + n（底下的事件數，見 detector.es._bucket_events；events: value 時就是 value）。
        筆數類（count、或 fill_zero: true）沒有資料的時段補 0：沒有請求就是 0，不是「不知道」。這也是為什麼資料不能缺，缺了會被學成「平常會掉到 0」。
        同一個指標不同語言名字不同的問題在收值端改名解決（docs/pdca/phase1-collect/2026-10-field-unify.md），這裡不分語言各查一次。"""
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
        body = {"size": 0, "query": {"bool": {"filter": filters}},
                "aggs": {"s": {"composite": {"size": 5000, "sources": sources}, "aggs": aggs}}}
        rows, after = [], None
        while True:
            if after:
                body["aggs"]["s"]["composite"]["after"] = after
            agg = self.es.req("POST", f"/{spec['index']}/{_SEARCH}", body).get("aggregations", {}).get("s", {})
            for b in agg.get("buckets", []):
                row = {f: b["key"][f"k{i}"] for i, f in enumerate(by)}
                row["ts"] = pd.Timestamp(b["key"]["ts"], unit="ms", tz="UTC") if interval else end
                row["value"] = _bucket_value(spec["value"], b, scale)
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
            alive = self.alive_times(spec, start, end, interval) if spec.get("filter") else None
            df = fill_zero(df, by, start, end, interval, alive)
        if spec.get("keys"):
            df = df.rename(columns=dict(zip(by, spec["keys"])))
        return df

    def alive_times(self, spec, start, end, interval):
        """收集系統有在送資料的時間窗：這條序列的 index、不加序列自己的 filter，任何一筆資料就算。
        整個 index 都沒有資料的時段＝收集系統停了（「不知道」），不是零流量。"""
        tf = spec.get("time_field", "@timestamp")
        body = {"size": 0, "query": {"bool": {"filter": [{"range": {tf: {"gte": start.isoformat(), "lt": end.isoformat()}}}]}},
                "aggs": {"t": {"composite": {"size": 10000, "sources": [{"ts": {"date_histogram": {"field": tf, "fixed_interval": interval}}}]}}}}
        out, after = [], None
        while True:
            if after:
                body["aggs"]["t"]["composite"]["after"] = after
            agg = self.es.req("POST", f"/{spec['index']}/{_SEARCH}", body).get("aggregations", {}).get("t", {})
            out += [b["key"]["ts"] for b in agg.get("buckets", [])]
            after = agg.get("after_key")
            if not after:
                break
        return pd.DatetimeIndex(pd.to_datetime(out, unit="ms", utc=True))

    # ── points（M3 靜態臨界值規則）──
    def points(self, rule, start, end):
        # exists：直方圖的 sum 這種子欄位不能拿來過濾「有沒有」，要用直方圖本身（例如 metrics.jvm_gc_duration_seconds）
        out = self._points(rule["index"], rule["field"], rule.get("filter", []), rule, start, end, rule.get("exists"))
        if rule.get("join"):
            # 分母在另一種文件：依「同一個對象、同一個時間點」補進來
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
        after = None
        while True:
            body = {"size": 5000, "_source": ["@timestamp", *fields, *keep], "query": q,
                    "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}]}
            if after:
                body["search_after"] = after
            hits = self.es.search(index, body)["hits"]["hits"]
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

    # ── spans（M4、根因分析）──
    def spans(self, index, trace_ids, start, end):
        """把這些 trace 的所有 span 讀回來（含其他服務的）。呼叫端把時間放寬，跨過邊界的請求也讀得完整。
        三種 index（core.traces.schema_of）回同一種 span 格式。"""
        out = []
        schema = schema_of(index)
        for i in range(0, len(trace_ids), 500):
            ids = trace_ids[i:i + 500]
            if schema == "merged":
                body = {"size": 500, "query": {"bool": {"filter": [time_range(start, end), {"terms": {"trace_id": ids}}]}}}
                for h in self.es.search(index, body)["hits"]["hits"]:
                    out += from_merged(h["_source"])
                continue
            # APM 的 transaction 沒有 span.id，分頁的次要排序只能用 _doc；同一毫秒的文件跨頁可能重複，最後依 span_id 去重
            src, sort, tid = ((_APM_FIELDS, [{"@timestamp": "asc"}, {"_doc": "asc"}], "trace.id") if schema == "apm"
                              else (_SPAN_FIELDS, [{"@timestamp": "asc"}, {"span_id": "asc"}], "trace_id"))
            after, seen = None, set()
            while True:
                body = {"size": 5000, "_source": src, "sort": sort,
                        "query": {"bool": {"filter": [time_range(start, end), {"terms": {tid: ids}}]}}}
                if after:
                    body["search_after"] = after
                hits = self.es.search(index, body)["hits"]["hits"]
                for h in hits:
                    s = to_span(h["_source"])
                    if s["span_id"] not in seen:
                        seen.add(s["span_id"])
                        out.append(s)
                if len(hits) < 5000:
                    break
                after = hits[-1]["sort"]
        return out

    def spans_by_id(self, index, span_ids, start, end):
        """用 span_id 讀回 span（例如 link 指到的、另一個 trace 裡的生產者）。"""
        out = {}
        want = set(span_ids)
        schema = schema_of(index)
        for i in range(0, len(span_ids), 1000):
            chunk = span_ids[i:i + 1000]
            if schema == "merged":
                body = {"size": 1000, "query": {"bool": {"filter": [time_range(start, end),
                                                                     {"nested": {"path": "spans", "query": {"terms": {"spans.span_id": chunk}}}}]}}}
                for h in self.es.search(index, body)["hits"]["hits"]:
                    out.update({s["span_id"]: s for s in from_merged(h["_source"]) if s["span_id"] in want})
            elif schema == "apm":   # transaction 的 id 在 transaction.id，span 的在 span.id
                body = {"size": 1000, "_source": _APM_FIELDS,
                        "query": {"bool": {"filter": [time_range(start, end)],
                                           "should": [{"terms": {"span.id": chunk}}, {"terms": {"transaction.id": chunk}}], "minimum_should_match": 1}}}
                out.update({s["span_id"]: s for s in (to_span(h["_source"]) for h in self.es.search(index, body)["hits"]["hits"]) if s["span_id"] in want})
            else:
                body = {"size": 1000, "_source": _SPAN_FIELDS,
                        "query": {"bool": {"filter": [time_range(start, end), {"terms": {"span_id": chunk}}]}}}
                out.update({h["_source"]["span_id"]: to_span(h["_source"]) for h in self.es.search(index, body)["hits"]["hits"]})
        return out

    # ── rows（ingest、paths-merge）──
    def source_rows(self, inp, start, end):
        """每列多帶 _source_doc_id（ES 的 _id）：ingest 算異常 _id 用。"""
        tf = inp.get("time_field", "@timestamp")
        filters = [{"range": {tf: {"gte": start.isoformat(), "lt": end.isoformat()}}}]
        if inp.get("filter"):
            filters.append(inp["filter"])
        out, after = [], None
        while True:
            body = {"size": 5000, "query": {"bool": {"filter": filters}}, "sort": [{tf: "asc"}, {"_doc": "asc"}]}
            if after:
                body["search_after"] = after
            hits = self.es.req("POST", f"/{inp['index']}/{_SEARCH}", body).get("hits", {}).get("hits", [])
            out += [{**h["_source"], "_source_doc_id": h["_id"]} for h in hits]
            if len(hits) < 5000:
                return out
            after = hits[-1]["sort"]


class ESBackend(ESSourceBackend):
    """讀寫都是 ES：設定沒寫 source（或 source、store 都是 es）時 open_backend 給的；as_backend 把 core.es.ES 包成這個。"""

    def __init__(self, es):
        super().__init__(es, ESStore(es))

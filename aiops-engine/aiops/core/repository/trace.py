"""trace：raw（traces-*.otel-*、traces-apm-*，每一段一筆）與合成一個請求一筆的 traces-merged-*（trace-merge 寫；工廠的履歷 lot-paths-* 格式一樣）。
索引名由設定給（cfg.indices.traces、trace.yaml 的 traces_index），三種格式（core/entity/trace.schema_of）回同一種 span。

「某個服務收到的請求」用 merged 的 nested entries（入口 span：服務、API、耗時、有沒有錯）挑，不用展開 spans。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from aiops.core.connection import time_range
from aiops.core.entity.trace import _APM_FIELDS, _SPAN_FIELDS, from_merged, is_merged, schema_of, to_span, trace_id_field
from aiops.core.repository._base import Repository


# ── 入口的查詢（merged）──

def entry_query(service: str, route: str | None, error: bool | None = None, start: datetime | None = None, end: datetime | None = None) -> dict:
    """「這個服務收到的請求」：entries 裡有一個是這個服務的 Server span（可以再限定 API、只要出錯的、入口開始的時間在 [start, end)）。
    時間放在入口上而不是文件的 @timestamp（＝整個請求開始）：IT 兩者差幾毫秒；工廠的履歷一個 lot 一筆、文件時間是 lot 開始，
    lot 幾小時後才到這一站，要看的是「幾點在這一站」。"""
    f = [{"term": {"entries.service": service}}, {"term": {"entries.kind": "Server"}}]
    if route:
        f.append({"term": {"entries.name": route}})
    if error is not None:
        f.append({"term": {"entries.error": error}})
    if start is not None and end is not None:
        f.append({"range": {"entries.start_ms": {"gte": start.timestamp() * 1000.0, "lt": end.timestamp() * 1000.0}}})
    return {"bool": {"filter": f}}


def entry_filter(service: str, route: str | None, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    return [{"nested": {"path": "entries", "query": entry_query(service, route, None, start, end)}}]


def _doc_range(start: datetime, end: datetime, lag: timedelta) -> dict:
    """文件層的時間框：入口可能比文件的 @timestamp 晚 lag（工廠：lot 開始到到這一站最多幾小時；IT 是 0），往前多框 lag。"""
    return time_range(start - lag, end)


def _entry_sort(service: str, route: str | None, slowest: bool, start: datetime | None = None, end: datetime | None = None) -> list:
    if not slowest:
        return [{"@timestamp": "desc"}]
    return [{"entries.duration_ms": {"order": "desc", "nested": {"path": "entries", "filter": entry_query(service, route, None, start, end)}}}]


class TraceRepository(Repository):
    """index 每次呼叫給（trace 的索引依場景、依設定）。"""

    # ── span ──
    def spans(self, index: str, trace_ids: list[str], start, end) -> list[dict]:
        """這些 trace 的所有 span（含其他服務的）。呼叫端把時間放寬，跨過邊界的請求也讀得完整。"""
        out = []
        schema = schema_of(index, self.scenario.source)
        for i in range(0, len(trace_ids), 500):
            ids = trace_ids[i:i + 500]
            if schema == "merged":
                body = {"size": 500, "query": {"bool": {"filter": [time_range(start, end), {"terms": {"trace_id": ids}}]}}}
                for h in self._hits(body, index):
                    out += from_merged(h["_source"])
                continue
            # APM 的 transaction 沒有 span.id，分頁的次要排序只能用 _doc；同一毫秒的文件跨頁可能重複，最後依 span_id 去重
            src, sort, tid = ((_APM_FIELDS, [{"@timestamp": "asc"}, {"_doc": "asc"}], "trace.id") if schema == "apm"
                              else (_SPAN_FIELDS, [{"@timestamp": "asc"}, {"span_id": "asc"}], "trace_id"))
            seen = set()
            body = {"_source": src, "query": {"bool": {"filter": [time_range(start, end), {"terms": {tid: ids}}]}}}
            for h in self._scan(body, index, sort=sort):
                s = to_span(h["_source"])
                if s["span_id"] not in seen:
                    seen.add(s["span_id"])
                    out.append(s)
        return out

    def spans_by_id(self, index: str, span_ids: list[str], start, end) -> dict[str, dict]:
        """用 span_id 讀回 span（例如 link 指到的、另一個 trace 裡的生產者）。"""
        out, want, schema = {}, set(span_ids), schema_of(index, self.scenario.source)
        for i in range(0, len(span_ids), 1000):
            chunk = span_ids[i:i + 1000]
            if schema == "merged":
                body = {"size": 1000, "query": {"bool": {"filter": [time_range(start, end),
                                                                     {"nested": {"path": "spans", "query": {"terms": {"spans.span_id": chunk}}}}]}}}
                for h in self._hits(body, index):
                    out.update({s["span_id"]: s for s in from_merged(h["_source"]) if s["span_id"] in want})
            elif schema == "apm":   # transaction 的 id 在 transaction.id，span 的在 span.id
                body = {"size": 1000, "_source": _APM_FIELDS,
                        "query": {"bool": {"filter": [time_range(start, end)],
                                           "should": [{"terms": {"span.id": chunk}}, {"terms": {"transaction.id": chunk}}], "minimum_should_match": 1}}}
                out.update({s["span_id"]: s for s in (to_span(h["_source"]) for h in self._hits(body, index)) if s["span_id"] in want})
            else:
                body = {"size": 1000, "_source": _SPAN_FIELDS, "query": {"bool": {"filter": [time_range(start, end), {"terms": {"span_id": chunk}}]}}}
                out.update({h["_source"]["span_id"]: to_span(h["_source"]) for h in self._hits(body, index)})
        return out

    # ── 合成（trace-merge、工廠 paths-merge）──
    def updated_trace_ids(self, raw_index: str, since: datetime, until: datetime) -> list[str]:
        """這段時間（依 event.ingested）有 span 進來的 trace。event.ingested 我們的 pipeline traces-otel 蓋、APM 的 Fleet pipeline 也蓋。"""
        body = {"size": 0, "query": {"range": {"event.ingested": {"gte": since.isoformat(), "lt": until.isoformat()}}},
                "aggs": {"t": {"composite": {"size": 2000, "sources": [{"id": {"terms": {"field": trace_id_field(raw_index, self.scenario.source)}}}]}}}}
        return [b["key"]["id"] for b in self._composite(body, ("t",), raw_index)]

    def write_merged(self, docs: dict[str, list[tuple[str, dict]]]) -> int:
        """{index: [(trace_id, 文件)]}（index 依 group 與日期分）。回寫了幾筆。"""
        for index, batch in docs.items():
            self.bulk(batch, index)
        return sum(len(b) for b in docs.values())

    def merged_docs(self, index: str, trace_ids: list[str], start: datetime, end: datetime) -> dict[str, dict]:
        """合成文件整筆（_id = trace_id）：{trace_id: 文件}。工廠的履歷（lot）用。"""
        out, ids = {}, sorted(trace_ids)
        for i in range(0, len(ids), 500):
            body = {"size": 500, "query": {"bool": {"filter": [time_range(start, end), {"terms": {"trace_id": ids[i:i + 500]}}]}}}
            for h in self._hits(body, index):
                out[h["_source"]["trace_id"]] = h["_source"]
        return out

    # ── 某個服務的請求（根因分析、事件整合）──
    def entry_stats(self, index: str, service: str, route: str | None, start: datetime, end: datetime, lag: timedelta = timedelta(0)) -> dict:
        """這個服務（這條 API）這段時間幾個請求、幾個出錯、p50／p95：都算在入口那一段上。"""
        q = entry_query(service, route, None, start, end)
        body = {"size": 0, "track_total_hits": True,
                "query": {"bool": {"filter": [_doc_range(start, end, lag), {"nested": {"path": "entries", "query": q}}]}},
                "aggs": {"n": {"nested": {"path": "entries"},
                               "aggs": {"m": {"filter": q,
                                              "aggs": {"p": {"percentiles": {"field": "entries.duration_ms", "percents": [50, 95]}},
                                                       "e": {"filter": {"term": {"entries.error": True}}}}}}}}}
        r = self._search(body, index)
        if "aggregations" not in r:     # 索引還不存在（工廠剛上線、還沒合成過履歷；allow_no_indices 回空的、沒有 aggregations）：當作沒有請求
            return {"requests": 0, "errors": 0, "p50_ms": 0, "p95_ms": 0}
        m = r["aggregations"]["n"]["m"]
        p = m["p"]["values"]
        return {"requests": m["doc_count"], "errors": m["e"]["doc_count"], "p50_ms": p.get("50.0") or 0, "p95_ms": p.get("95.0") or 0}

    def sample_trace_ids(self, index: str, service: str, route: str | None, start: datetime, end: datetime,
                         n: int, slowest: bool, lag: timedelta = timedelta(0)) -> list[str]:
        """出事時：挑最慢的、和出錯的請求；平常：挑最近的請求（當作「正常的樣子」）。"""
        def ids(error):
            q = {"bool": {"filter": [_doc_range(start, end, lag), {"nested": {"path": "entries", "query": entry_query(service, route, error, start, end)}}]}}
            hits = self._hits({"size": n if error is None else n // 2, "query": q, "sort": _entry_sort(service, route, slowest, start, end),
                               "_source": ["trace_id"]}, index)
            return [h["_source"]["trace_id"] for h in hits]
        out = ids(None)
        if slowest:
            out += ids(True)
        return list(dict.fromkeys(out))

    def recent_trace_ids(self, index: str, start: datetime, end: datetime, n: int) -> list[str]:
        """最近的請求（整個 trace 的根是 Server span 的）。"""
        hits = self._hits({"size": n, "_source": ["trace_id"], "sort": [{"@timestamp": "desc"}],
                           "query": {"bool": {"filter": [time_range(start, end), {"term": {"root.kind": "Server"}}]}}}, index)
        return list(dict.fromkeys(h["_source"]["trace_id"] for h in hits))

    def service_trace_ids(self, index: str, service: str, start: datetime, end: datetime, n: int, lag: timedelta = timedelta(0)) -> list[str]:
        """這個服務最近收到的 n 個請求（Server span）的 trace_id：事件整合算「誰呼叫誰」用。"""
        hits = self._hits({"size": n, "_source": ["trace_id"], "sort": [{"@timestamp": "desc"}],
                           "query": {"bool": {"filter": [_doc_range(start, end, lag), *entry_filter(service, None, start, end)]}}}, index)
        return list(dict.fromkeys(h["_source"]["trace_id"] for h in hits))

    # ── 抽樣（trace 每段的統計）──
    def sample_entries(self, index: str, start: pd.Timestamp, end: pd.Timestamp, interval: str, per_bucket: int,
                       policy: str | None = None) -> list[tuple[pd.Timestamp, str, str]]:
        """每個時段、每個入口（服務的 Server span、訊息消費者的 Consumer span、沒有上一層的 span）抽 per_bucket 個請求：
        [(時段, trace_id, span_id)]。依隨機的 id 排序取前幾個，等於隨機抽。merged 走 _sample_entries_merged（入口用合成時算好的 entries）；
        raw（每一段一筆）照舊。policy：以前尾端取樣時只抽 random 那份；現在 100% 收，trace.yaml 不設這個，全部都抽。"""
        if is_merged(index):
            return self._sample_entries_merged(index, start, end, interval, per_bucket)
        sources = [{"svc": {"terms": {"field": "resource.attributes.service.name"}}},
                   {"name": {"terms": {"field": "name"}}},
                   {"ts": {"date_histogram": {"field": "@timestamp", "fixed_interval": interval}}}]
        # 入口：HTTP 請求（Server）、訊息消費者（Consumer），以及「沒有上一層的 span」：
        # .NET 自動埋點的 Kafka 消費者處理訊息時查 DB，那個 DB 的 span 沒有上一層、自成一個 trace（串不回訊息），
        # 也當成入口算，至少看得到「這個服務查 DB 變慢、出錯」（docs/trace/README.md 場景四）
        entry = {"bool": {"should": [{"terms": {"kind": ["Server", "Consumer"]}},
                                     {"bool": {"must_not": {"exists": {"field": "parent_span_id"}}}}], "minimum_should_match": 1}}
        filters = [time_range(start, end), entry]
        if policy:
            f = "scope.attributes.tailsampling.policy"
            filters.append({"bool": {"should": [{"term": {f: policy}}, {"bool": {"must_not": {"exists": {"field": f}}}}]}})
        body = {"size": 0, "query": {"bool": {"filter": filters}},
                "aggs": {"e": {"composite": {"size": 1000, "sources": sources},
                               "aggs": {"t": {"top_hits": {"size": per_bucket, "sort": [{"trace_id": "asc"}], "_source": ["trace_id", "span_id"]}}}}}}
        return [(pd.Timestamp(b["key"]["ts"], unit="ms", tz="UTC"), h["_source"]["trace_id"], h["_source"]["span_id"])
                for b in self._composite(body, ("e",), index) for h in b["t"]["hits"]["hits"]]

    def _sample_entries_merged(self, index: str, start: pd.Timestamp, end: pd.Timestamp, interval: str, per_bucket: int) -> list[tuple]:
        """merged：一筆是一個請求，entries 是合成時算好的入口（nested）。nested → composite（服務 × API × 時段）→ top_hits：
        hit 的 _id 是整筆的 _id（= trace_id）、_source 是那個入口的 span_id；依 span_id 排序（隨機的 16 進位）等於隨機抽。
        時段用入口自己的 start_ms 切（nested 裡拿不到整筆的 @timestamp）；查詢的時間範圍兩邊各放寬 5 分鐘，再只留 [start, end) 的時段。"""
        step_ms = int(pd.Timedelta(interval if interval.endswith("in") else interval.replace("m", "min")).total_seconds() * 1000)
        pad = pd.Timedelta(minutes=5)
        sources = [{"svc": {"terms": {"field": "entries.service"}}},
                   {"name": {"terms": {"field": "entries.name"}}},
                   {"ts": {"histogram": {"field": "entries.start_ms", "interval": step_ms}}}]
        body = {"size": 0, "query": {"bool": {"filter": [time_range(start - pad, end + pad)]}},
                "aggs": {"n": {"nested": {"path": "entries"},
                               "aggs": {"e": {"composite": {"size": 1000, "sources": sources},
                                              "aggs": {"t": {"top_hits": {"size": per_bucket, "sort": [{"entries.span_id": "asc"}],
                                                                          "_source": ["entries.span_id"]}}}}}}}}
        lo, hi = start.value // 10**6, end.value // 10**6
        out = []
        for b in self._composite(body, ("n", "e"), index):
            ms = int(b["key"]["ts"])
            if lo <= ms < hi:
                out += [(pd.Timestamp(ms, unit="ms", tz="UTC"), h["_id"], h["_source"]["span_id"]) for h in b["t"]["hits"]["hits"]]
        return out

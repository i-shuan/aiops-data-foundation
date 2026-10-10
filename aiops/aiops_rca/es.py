"""根因分析從 ES 讀的資料：baseline 找到的異常、某個服務的請求、log。連線見 aiops_common.es，span 見 aiops_common.traces。"""
from __future__ import annotations

from datetime import datetime

from aiops_common.es import ES, get_field, time_range


# ── 1. metrics：baseline 找到的異常 ──

def anomalies(es: ES, index: str, start: datetime, end: datetime, service: str | None = None) -> list[dict]:
    f = [time_range(start, end)]
    if service:
        f.append({"bool": {"should": [{"term": {"keys.resource.attributes.service.name": service}},
                                      {"term": {"keys.entry.service": service}}], "minimum_should_match": 1}})
    hits = es.search(index, {"size": 1000, "query": {"bool": {"filter": f}}, "sort": [{"@timestamp": "asc"}]})["hits"]["hits"]
    return [h["_source"] for h in hits]


# ── 2. trace ──

def entry_filter(service: str, route: str | None) -> list[dict]:
    """「這個服務收到的請求」：服務的 Server span，可以再限定 API。"""
    f = [{"term": {"resource.attributes.service.name": service}}, {"term": {"kind": "Server"}}]
    if route:
        f.append({"term": {"name": route}})
    return f


def entry_stats(es: ES, index: str, service: str, route: str | None, start: datetime, end: datetime) -> dict:
    body = {"size": 0, "track_total_hits": True, "query": {"bool": {"filter": [time_range(start, end), *entry_filter(service, route)]}},
            "aggs": {"p": {"percentiles": {"field": "duration", "percents": [50, 95]}},
                     "e": {"filter": {"term": {"status.code": "Error"}}}}}
    r = es.search(index, body)
    p = r["aggregations"]["p"]["values"]
    n = r["hits"]["total"]["value"]
    return {"requests": n, "errors": r["aggregations"]["e"]["doc_count"],
            "p50_ms": (p.get("50.0") or 0) / 1e6, "p95_ms": (p.get("95.0") or 0) / 1e6}


def sample_trace_ids(es: ES, index: str, service: str, route: str | None, start: datetime, end: datetime,
                     n: int, slowest: bool) -> list[str]:
    """出事時：挑最慢的、和出錯的請求；平常：挑最近的請求（當作「正常的樣子」）。"""
    q = {"bool": {"filter": [time_range(start, end), *entry_filter(service, route)]}}
    sort = [{"duration": "desc"}] if slowest else [{"@timestamp": "desc"}]
    ids = [h["_source"]["trace_id"] for h in
           es.search(index, {"size": n, "query": q, "sort": sort, "_source": ["trace_id"]})["hits"]["hits"]]
    if slowest:
        q["bool"]["filter"].append({"term": {"status.code": "Error"}})
        ids += [h["_source"]["trace_id"] for h in
                es.search(index, {"size": n // 2, "query": q, "sort": sort, "_source": ["trace_id"]})["hits"]["hits"]]
    return list(dict.fromkeys(ids))


def recent_trace_ids(es: ES, index: str, start: datetime, end: datetime, n: int) -> list[str]:
    hits = es.search(index, {"size": n, "_source": ["trace_id"], "sort": [{"@timestamp": "desc"}],
                             "query": {"bool": {"filter": [time_range(start, end), {"term": {"kind": "Server"}}],
                                                "must_not": [{"exists": {"field": "parent_span_id"}}]}}})["hits"]["hits"]
    return list(dict.fromkeys(h["_source"]["trace_id"] for h in hits))


# ── 3. log ──

def _to_log(src: dict) -> dict:
    return {"ts": src.get("@timestamp"), "service": get_field(src, "service.name") or "?", "level": get_field(src, "log.level"),
            "trace_id": get_field(src, "trace.id"), "message": src.get("message") or ""}


def logs_of_traces(es: ES, index: str, trace_ids: list[str], start: datetime, end: datetime, size: int = 2000) -> list[dict]:
    """這些請求在各服務留下的 log（log 的 trace.id = trace 的 trace_id）。"""
    if not trace_ids:
        return []
    body = {"size": size, "sort": [{"@timestamp": "asc"}],
            "_source": ["@timestamp", "service.name", "log.level", "trace.id", "message"],
            "query": {"bool": {"filter": [time_range(start, end), {"terms": {"trace.id": trace_ids[:1000]}}]}}}
    return [_to_log(h["_source"]) for h in es.search(index, body)["hits"]["hits"]]


def problem_logs(es: ES, index: str, services: list[str], start: datetime, end: datetime, size: int = 5000) -> list[dict]:
    """這些服務在這段時間的 warn、error、fatal log（不限有沒有 trace.id：有些錯誤不在請求裡，例如背景工作）。"""
    body = {"size": size, "_source": ["@timestamp", "service.name", "log.level", "trace.id", "message"],
            "query": {"bool": {"filter": [time_range(start, end), {"terms": {"service.name": services}},
                                          {"terms": {"log.level": ["warn", "error", "fatal"]}}]}}}
    return [_to_log(h["_source"]) for h in es.search(index, body)["hits"]["hits"]]

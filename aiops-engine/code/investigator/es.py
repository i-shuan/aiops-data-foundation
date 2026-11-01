"""根因分析從 ES 讀的資料：baseline 找到的異常、某個服務的請求、log。連線見 core.es，span 見 core.traces。

trace 讀合成一筆的 traces-merged-*（detector trace-merge 寫的；事故可能幾天後才查，raw 只留 2 天）：
「某個服務收到的請求」用 nested 的 entries（入口 span：服務、API、耗時、有沒有錯）挑，不用展開 spans。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core.es import ES, time_range


# ── 1. metrics：baseline 找到的異常 ──

def anomalies(es: ES, index: str, start: datetime, end: datetime, service: str | None = None) -> list[dict]:
    f = [time_range(start, end)]
    if service:
        f.append({"bool": {"should": [{"term": {"keys.resource.attributes.service.name": service}},
                                      {"term": {"keys.entry.service": service}},
                                      {"term": {"keys.service": service}}], "minimum_should_match": 1}})
    hits = es.search(index, {"size": 1000, "query": {"bool": {"filter": f}}, "sort": [{"@timestamp": "asc"}]})["hits"]["hits"]
    return [{**h["_source"], "_id": h["_id"]} for h in hits]


# ── 2. trace（merged）──

def entry_query(service: str, route: str | None, error: bool | None = None) -> dict:
    """「這個服務收到的請求」：entries 裡有一個是這個服務的 Server span（可以再限定 API、只要出錯的）。"""
    f = [{"term": {"entries.service": service}}, {"term": {"entries.kind": "Server"}}]
    if route:
        f.append({"term": {"entries.name": route}})
    if error is not None:
        f.append({"term": {"entries.error": error}})
    return {"bool": {"filter": f}}


def entry_filter(service: str, route: str | None) -> list[dict]:
    return [{"nested": {"path": "entries", "query": entry_query(service, route)}}]


def entry_stats(es: ES, index: str, service: str, route: str | None, start: datetime, end: datetime) -> dict:
    """這個服務（這條 API）這段時間幾個請求、幾個出錯、p50／p95：都算在入口那一段上。"""
    q = entry_query(service, route)
    body = {"size": 0, "track_total_hits": True,
            "query": {"bool": {"filter": [time_range(start, end), {"nested": {"path": "entries", "query": q}}]}},
            "aggs": {"n": {"nested": {"path": "entries"},
                           "aggs": {"m": {"filter": q,
                                          "aggs": {"p": {"percentiles": {"field": "entries.duration_ms", "percents": [50, 95]}},
                                                   "e": {"filter": {"term": {"entries.error": True}}}}}}}}}
    r = es.search(index, body)
    m = r["aggregations"]["n"]["m"]
    p = m["p"]["values"]
    return {"requests": m["doc_count"], "errors": m["e"]["doc_count"],
            "p50_ms": p.get("50.0") or 0, "p95_ms": p.get("95.0") or 0}


def _entry_sort(service: str, route: str | None, slowest: bool) -> list:
    if not slowest:
        return [{"@timestamp": "desc"}]
    return [{"entries.duration_ms": {"order": "desc", "nested": {"path": "entries", "filter": entry_query(service, route)}}}]


def sample_trace_ids(es: ES, index: str, service: str, route: str | None, start: datetime, end: datetime,
                     n: int, slowest: bool) -> list[str]:
    """出事時：挑最慢的、和出錯的請求；平常：挑最近的請求（當作「正常的樣子」）。"""
    def ids(error):
        q = {"bool": {"filter": [time_range(start, end), {"nested": {"path": "entries", "query": entry_query(service, route, error)}}]}}
        hits = es.search(index, {"size": n if error is None else n // 2, "query": q, "sort": _entry_sort(service, route, slowest),
                                 "_source": ["trace_id"]})["hits"]["hits"]
        return [h["_source"]["trace_id"] for h in hits]
    out = ids(None)
    if slowest:
        out += ids(True)
    return list(dict.fromkeys(out))


def recent_trace_ids(es: ES, index: str, start: datetime, end: datetime, n: int) -> list[str]:
    """最近的請求（整個 trace 的根是 Server span 的）。"""
    hits = es.search(index, {"size": n, "_source": ["trace_id"], "sort": [{"@timestamp": "desc"}],
                             "query": {"bool": {"filter": [time_range(start, end), {"term": {"root.kind": "Server"}}]}}})["hits"]["hits"]
    return list(dict.fromkeys(h["_source"]["trace_id"] for h in hits))


def service_trace_ids(es: ES, index: str, service: str, start: datetime, end: datetime, n: int) -> list[str]:
    """這個服務最近收到的 n 個請求（Server span）的 trace_id：事件整合算「誰呼叫誰」用（cmd_alert.call_edges）。"""
    hits = es.search(index, {"size": n, "_source": ["trace_id"], "sort": [{"@timestamp": "desc"}],
                             "query": {"bool": {"filter": [time_range(start, end), *entry_filter(service, None)]}}})["hits"]["hits"]
    return list(dict.fromkeys(h["_source"]["trace_id"] for h in hits))


# ── 3. log ──

LOG_FIELDS = ["@timestamp", "resource.attributes.service.name", "log.level", "trace_id", "message"]


def _to_log(hit: dict) -> dict:
    """log 的 resource.attributes.service.name、trace_id 是 alias（跟 metrics、trace 同一條路徑），_source 裡沒有，要用 fields 取。"""
    f = {k: (v[0] if isinstance(v, list) and v else v) for k, v in hit.get("fields", {}).items()}
    return {"ts": f.get("@timestamp"), "service": f.get("resource.attributes.service.name") or "?", "level": f.get("log.level"),
            "trace_id": f.get("trace_id"), "message": f.get("message") or ""}


# log 的 @timestamp 是應用自己的時間：時鐘偏了、或請求結束後才寫的 log，可能離 span 十幾分鐘。
# trace_id 本身就夠準，時間只用來少掃幾個 backing index，所以前後多放 30 分鐘（docs/pdca/phase1-collect/2026-10-log-trace-bind.md）
LOG_TRACE_SKEW = timedelta(minutes=30)


def logs_of_traces(es: ES, index: str, trace_ids: list[str], start: datetime, end: datetime, size: int = 2000) -> list[dict]:
    """這些請求在各服務留下的 log（log 的 trace_id 是 alias，指到 trace.id；跟 trace 的 trace_id 同名）。"""
    if not trace_ids:
        return []
    body = {"size": size, "sort": [{"@timestamp": "asc"}], "_source": False, "fields": LOG_FIELDS,
            "query": {"bool": {"filter": [time_range(start - LOG_TRACE_SKEW, end + LOG_TRACE_SKEW),
                                          {"terms": {"trace_id": trace_ids[:1000]}}]}}}
    return [_to_log(h) for h in es.search(index, body)["hits"]["hits"]]


def problem_logs(es: ES, index: str, services: list[str], start: datetime, end: datetime, size: int = 5000) -> list[dict]:
    """這些服務在這段時間的 warn、error、fatal log（不限有沒有 trace_id：有些錯誤不在請求裡，例如背景工作）。"""
    body = {"size": size, "_source": False, "fields": LOG_FIELDS,
            "query": {"bool": {"filter": [time_range(start, end), {"terms": {"resource.attributes.service.name": services}},
                                          {"terms": {"log.level": ["warn", "error", "fatal"]}}]}}}
    return [_to_log(h) for h in es.search(index, body)["hits"]["hits"]]

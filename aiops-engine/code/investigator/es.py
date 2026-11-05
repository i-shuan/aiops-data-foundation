"""根因分析從 ES 讀的資料：baseline 找到的異常、某個服務的請求、log。連線見 core.es，span 見 sources.traces。

trace 讀合成一筆的 traces-merged-*（detector trace-merge 寫的；事故可能幾天後才查，raw 只留 2 天）：
「某個服務收到的請求」用 nested 的 entries（入口 span：服務、API、耗時、有沒有錯）挑，不用展開 spans。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core import plugin, source
from core.es import ES, time_range


# ── 1. metrics：baseline 找到的異常 ──

def anomalies(es: ES, index: str, start: datetime, end: datetime, service: str | None = None) -> list[dict]:
    f = [time_range(start, end)]
    if service:   # 這個對象的異常：keys 裡哪幾個欄位是對象由 plugin 決定（服務／機台）
        f.append({"bool": {"should": [{"term": {f"keys.{k}": service}} for k in plugin.current().entity_keys], "minimum_should_match": 1}})
    hits = es.search(index, {"size": 1000, "query": {"bool": {"filter": f}}, "sort": [{"@timestamp": "asc"}]})["hits"]["hits"]
    return [{**h["_source"], "_id": h["_id"]} for h in hits]


# ── 2. trace（merged）──

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


def entry_stats(es: ES, index: str, service: str, route: str | None, start: datetime, end: datetime, lag: timedelta = timedelta(0)) -> dict:
    """這個服務（這條 API）這段時間幾個請求、幾個出錯、p50／p95：都算在入口那一段上。"""
    q = entry_query(service, route, None, start, end)
    body = {"size": 0, "track_total_hits": True,
            "query": {"bool": {"filter": [_doc_range(start, end, lag), {"nested": {"path": "entries", "query": q}}]}},
            "aggs": {"n": {"nested": {"path": "entries"},
                           "aggs": {"m": {"filter": q,
                                          "aggs": {"p": {"percentiles": {"field": "entries.duration_ms", "percents": [50, 95]}},
                                                   "e": {"filter": {"term": {"entries.error": True}}}}}}}}}
    r = es.search(index, body)
    if "aggregations" not in r:     # 索引還不存在（工廠剛上線、還沒合成過履歷；allow_no_indices 回空的、沒有 aggregations）：當作沒有請求
        return {"requests": 0, "errors": 0, "p50_ms": 0, "p95_ms": 0}
    m = r["aggregations"]["n"]["m"]
    p = m["p"]["values"]
    return {"requests": m["doc_count"], "errors": m["e"]["doc_count"],
            "p50_ms": p.get("50.0") or 0, "p95_ms": p.get("95.0") or 0}


def _entry_sort(service: str, route: str | None, slowest: bool, start: datetime | None = None, end: datetime | None = None) -> list:
    if not slowest:
        return [{"@timestamp": "desc"}]
    return [{"entries.duration_ms": {"order": "desc", "nested": {"path": "entries", "filter": entry_query(service, route, None, start, end)}}}]


def sample_trace_ids(es: ES, index: str, service: str, route: str | None, start: datetime, end: datetime,
                     n: int, slowest: bool, lag: timedelta = timedelta(0)) -> list[str]:
    """出事時：挑最慢的、和出錯的請求；平常：挑最近的請求（當作「正常的樣子」）。"""
    def ids(error):
        q = {"bool": {"filter": [_doc_range(start, end, lag), {"nested": {"path": "entries", "query": entry_query(service, route, error, start, end)}}]}}
        hits = es.search(index, {"size": n if error is None else n // 2, "query": q, "sort": _entry_sort(service, route, slowest, start, end),
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


def service_trace_ids(es: ES, index: str, service: str, start: datetime, end: datetime, n: int, lag: timedelta = timedelta(0)) -> list[str]:
    """這個服務最近收到的 n 個請求（Server span）的 trace_id：事件整合算「誰呼叫誰」用（plugins/observability/chain.call_edges）。"""
    hits = es.search(index, {"size": n, "_source": ["trace_id"], "sort": [{"@timestamp": "desc"}],
                             "query": {"bool": {"filter": [_doc_range(start, end, lag), *entry_filter(service, None, start, end)]}}})["hits"]["hits"]
    return list(dict.fromkeys(h["_source"]["trace_id"] for h in hits))


# ── 3. log ──

def _log_fields() -> dict:
    """log 的欄位名依資料來源（core/source.py 的 log）：我們的 k8s-* 是 resource.attributes.service.name、trace_id（alias，
    _source 裡沒有，要用 fields 取）；APM 的 logs-apm.* 是 service.name、trace.id。"""
    return source.profile("log")


def _to_log(hit: dict) -> dict:
    p = _log_fields()
    f = {k: (v[0] if isinstance(v, list) and v else v) for k, v in hit.get("fields", {}).items()}
    return {"ts": f.get("@timestamp"), "service": f.get(p["service"]) or "?", "level": f.get(p["level"]),
            "trace_id": f.get(p["trace_id"]), "message": next((f[m] for m in p["message"] if f.get(m)), "")}


def _fields_param(p: dict) -> list[str]:
    return ["@timestamp", p["service"], p["level"], p["trace_id"], *p["message"]]


# log 的 @timestamp 是應用自己的時間：時鐘偏了、或請求結束後才寫的 log，可能離 span 十幾分鐘。
# trace_id 本身就夠準，時間只用來少掃幾個 backing index，所以前後多放 30 分鐘（docs/pdca/phase1-collect/2026-10-log-trace-bind.md）
LOG_TRACE_SKEW = timedelta(minutes=30)


def logs_of_traces(es: ES, index: str, trace_ids: list[str], start: datetime, end: datetime, size: int = 2000) -> list[dict]:
    """這些請求在各服務留下的 log（log 的 trace_id 是 alias，指到 trace.id；跟 trace 的 trace_id 同名）。"""
    if not trace_ids:
        return []
    p = _log_fields()
    body = {"size": size, "sort": [{"@timestamp": "asc"}], "_source": False, "fields": _fields_param(p),
            "query": {"bool": {"filter": [time_range(start - LOG_TRACE_SKEW, end + LOG_TRACE_SKEW),
                                          {"terms": {p["trace_id"]: trace_ids[:1000]}}]}}}
    return [_to_log(h) for h in es.search(index, body)["hits"]["hits"]]


def change_logs(es: ES, index: str, services: list[str], start: datetime, end: datetime, query: dict, size: int = 200) -> list[dict]:
    """這些對象在這段時間的變更紀錄（工廠：PM、recipe 變更；query 是哪些算變更，例如 {terms: {event_type: [pm, recipe_change]}}），依時間排。"""
    p = _log_fields()
    body = {"size": size, "_source": False, "fields": _fields_param(p), "sort": [{"@timestamp": "asc"}],
            "query": {"bool": {"filter": [time_range(start, end), {"terms": {p["service"]: services}}, query]}}}
    return [_to_log(h) for h in es.search(index, body)["hits"]["hits"]]


def problem_logs(es: ES, index: str, services: list[str], start: datetime, end: datetime, size: int = 5000) -> list[dict]:
    """這些服務在這段時間的 warn、error、fatal log（不限有沒有 trace_id：有些錯誤不在請求裡，例如背景工作）。
    來源有 problem_query 的（APM 的例外文件沒有 log.level，用 processor.event: error 認）一起撈。"""
    p = _log_fields()
    level = {"terms": {p["level"]: p["problem_levels"]}}
    which = {"bool": {"should": [level, p["problem_query"]], "minimum_should_match": 1}} if p.get("problem_query") else level
    body = {"size": size, "_source": False, "fields": _fields_param(p),
            "query": {"bool": {"filter": [time_range(start, end), {"terms": {p["service"]: services}}, which]}}}
    return [_to_log(h) for h in es.search(index, body)["hits"]["hits"]]

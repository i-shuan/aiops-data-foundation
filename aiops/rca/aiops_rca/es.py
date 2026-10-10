"""從 ES 讀根因分析要的資料：baseline 找到的異常、trace 的 span、log。

連線用環境變數（跟 aiops/baseline 一樣）：
  ES_URL                       例如 https://es.example.internal:9200
  ES_API_KEY 或 ES_AUTH        API key 的 encoded，或 帳號:密碼（用 baseline 的 key 就可以：能讀 log、metrics、trace 和 aiops-*）
  ES_CA（選填）                ES 憑證的 CA 檔
"""
from __future__ import annotations

import os
from datetime import datetime

import requests


class ES:
    def __init__(self, url: str | None = None):
        self.url = (url or os.environ["ES_URL"]).rstrip("/")
        self.s = requests.Session()
        if os.environ.get("ES_API_KEY"):
            self.s.headers["Authorization"] = f"ApiKey {os.environ['ES_API_KEY']}"
        elif os.environ.get("ES_AUTH"):
            self.s.auth = tuple(os.environ["ES_AUTH"].split(":", 1))
        if os.environ.get("ES_CA"):
            self.s.verify = os.environ["ES_CA"]

    def search(self, index: str, body: dict) -> dict:
        r = self.s.post(f"{self.url}/{index}/_search?ignore_unavailable=true&allow_no_indices=true", json=body, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"查詢 {index} 失敗：{r.status_code} {r.text[:500]}")
        return r.json()


def _get(d, dotted: str):
    """ES 的 _source 裡，同一個欄位可能是巢狀（{"db": {"system": ...}}）、攤平（{"db.system": ...}），或兩者混用。"""
    if not isinstance(d, dict):
        return None
    if dotted in d:
        return d[dotted]
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        head = ".".join(parts[:i])
        if head in d:
            v = _get(d[head], ".".join(parts[i:]))
            if v is not None:
                return v
    return None


def _rng(start: datetime, end: datetime, field: str = "@timestamp") -> dict:
    return {"range": {field: {"gte": start.isoformat(), "lt": end.isoformat()}}}


# ── 1. metrics：baseline 找到的異常 ──

def anomalies(es: ES, index: str, start: datetime, end: datetime, service: str | None = None) -> list[dict]:
    f = [_rng(start, end)]
    if service:
        f.append({"term": {"keys.resource.attributes.service.name": service}})
    hits = es.search(index, {"size": 1000, "query": {"bool": {"filter": f}}, "sort": [{"@timestamp": "asc"}]})["hits"]["hits"]
    return [h["_source"] for h in hits]


# ── 2. trace ──

_SPAN_FIELDS = ["@timestamp", "trace_id", "span_id", "parent_span_id", "name", "kind", "duration", "status.code",
                "resource.attributes.service.name", "attributes"]


def to_span(src: dict) -> dict:
    attrs = src.get("attributes") or {}
    target = _get(attrs, "peer.service") or _get(attrs, "db.system") or _get(attrs, "server.address")
    ts = src["@timestamp"]
    start_ms = float(ts) if isinstance(ts, (int, float)) else datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
    return {"trace_id": src["trace_id"], "span_id": src["span_id"], "parent": src.get("parent_span_id") or None,
            "service": _get(src, "resource.attributes.service.name") or "?", "name": src.get("name", "?"),
            "kind": src.get("kind", ""), "target": target, "start_ms": start_ms,
            "dur_ms": (src.get("duration") or 0) / 1e6, "error": _get(src, "status.code") == "Error"}


def entry_filter(service: str, route: str | None) -> list[dict]:
    """「這個服務收到的請求」：服務的 Server span，可以再限定 API。"""
    f = [{"term": {"resource.attributes.service.name": service}}, {"term": {"kind": "Server"}}]
    if route:
        f.append({"term": {"name": route}})
    return f


def entry_stats(es: ES, index: str, service: str, route: str | None, start: datetime, end: datetime) -> dict:
    body = {"size": 0, "track_total_hits": True, "query": {"bool": {"filter": [_rng(start, end), *entry_filter(service, route)]}},
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
    q = {"bool": {"filter": [_rng(start, end), *entry_filter(service, route)]}}
    sort = [{"duration": "desc"}] if slowest else [{"@timestamp": "desc"}]
    ids = [h["_source"]["trace_id"] for h in
           es.search(index, {"size": n, "query": q, "sort": sort, "_source": ["trace_id"]})["hits"]["hits"]]
    if slowest:
        q["bool"]["filter"].append({"term": {"status.code": "Error"}})
        ids += [h["_source"]["trace_id"] for h in
                es.search(index, {"size": n // 2, "query": q, "sort": sort, "_source": ["trace_id"]})["hits"]["hits"]]
    return list(dict.fromkeys(ids))


def spans_of(es: ES, index: str, trace_ids: list[str], start: datetime, end: datetime) -> list[dict]:
    """把這些 trace 的所有 span 讀回來（含其他服務的）。時間放寬，跨過邊界的請求也讀得完整。"""
    out = []
    for i in range(0, len(trace_ids), 500):
        after = None
        while True:
            body = {"size": 5000, "_source": _SPAN_FIELDS, "sort": [{"@timestamp": "asc"}, {"span_id": "asc"}],
                    "query": {"bool": {"filter": [_rng(start, end), {"terms": {"trace_id": trace_ids[i:i + 500]}}]}}}
            if after:
                body["search_after"] = after
            hits = es.search(index, body)["hits"]["hits"]
            out += [to_span(h["_source"]) for h in hits]
            if len(hits) < 5000:
                break
            after = hits[-1]["sort"]
    return out


def recent_trace_ids(es: ES, index: str, start: datetime, end: datetime, n: int) -> list[str]:
    hits = es.search(index, {"size": n, "_source": ["trace_id"], "sort": [{"@timestamp": "desc"}],
                             "query": {"bool": {"filter": [_rng(start, end), {"term": {"kind": "Server"}}],
                                                "must_not": [{"exists": {"field": "parent_span_id"}}]}}})["hits"]["hits"]
    return list(dict.fromkeys(h["_source"]["trace_id"] for h in hits))


# ── 3. log ──

def _to_log(src: dict) -> dict:
    return {"ts": src.get("@timestamp"), "service": _get(src, "service.name") or "?", "level": _get(src, "log.level"),
            "trace_id": _get(src, "trace.id"), "message": src.get("message") or ""}


def logs_of_traces(es: ES, index: str, trace_ids: list[str], start: datetime, end: datetime, size: int = 2000) -> list[dict]:
    """這些請求在各服務留下的 log（log 的 trace.id = trace 的 trace_id）。"""
    if not trace_ids:
        return []
    body = {"size": size, "sort": [{"@timestamp": "asc"}],
            "_source": ["@timestamp", "service.name", "log.level", "trace.id", "message"],
            "query": {"bool": {"filter": [_rng(start, end), {"terms": {"trace.id": trace_ids[:1000]}}]}}}
    return [_to_log(h["_source"]) for h in es.search(index, body)["hits"]["hits"]]


def problem_logs(es: ES, index: str, services: list[str], start: datetime, end: datetime, size: int = 5000) -> list[dict]:
    """這些服務在這段時間的 warn、error、fatal log（不限有沒有 trace.id：有些錯誤不在請求裡，例如背景工作）。"""
    body = {"size": size, "_source": ["@timestamp", "service.name", "log.level", "trace.id", "message"],
            "query": {"bool": {"filter": [_rng(start, end), {"terms": {"service.name": services}},
                                          {"terms": {"log.level": ["warn", "error", "fatal"]}}]}}}
    return [_to_log(h["_source"]) for h in es.search(index, body)["hits"]["hits"]]

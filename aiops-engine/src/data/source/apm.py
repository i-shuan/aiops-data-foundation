"""Elastic APM（Fleet 的 apm integration）：metrics-apm.*、traces-apm-*、logs-apm.*。

transaction、span 各一筆（processor.event 區分）；欄位是 ECS 的扁平寫法（service.name、trace.id、parent.id、*.duration.us、event.outcome）；
耗時是微秒；沒有 OTel 的 kind；例外是 logs-apm.error 的獨立文件。轉成統一的 span 後，後面跟 OTel 來源一樣。
欄位對照與差異：aiops-engine/configs/apm/README.md。
"""
from __future__ import annotations

from common.infra.es import get_field
from data.source.adapter import SourceAdapter, duration_ms, start_ms

# APM 的文件：transaction、span 都是物件（span.stacktrace 很大、不要）；labels 是 agent 設的標籤（dept、group、業務 ID）
_APM_FIELDS = {"includes": ["@timestamp", "timestamp.us", "processor.event", "trace.id", "parent.id", "transaction", "span",
                            "event.outcome", "service", "kubernetes", "host.name", "labels", "data_stream.namespace",
                            "http.request.method", "http.response.status_code", "url.path"],
               "excludes": ["span.stacktrace", "transaction.custom", "transaction.marks"]}


def _apm_kind(event: str, obj: dict) -> str:
    """APM 沒有 OTel 的 kind，從文件種類推：transaction 是入口（HTTP 請求 Server、訊息處理 Consumer）；
    span 有往外的對象（DB、下游、外部系統）是 Client，送訊息是 Producer，其餘是程式內部的段（Internal）。"""
    typ = (get_field(obj, "type") or "").split(".")[0]
    if event == "transaction":
        return "Consumer" if typ == "messaging" else "Server"
    if typ == "messaging" and (get_field(obj, "action") or "") in ("send", "publish", "produce"):
        return "Producer"
    if typ in ("db", "external", "storage", "messaging") or get_field(obj, "destination.service.resource"):
        return "Client"
    return "Internal"


def _apm_span(src: dict) -> dict:
    """Elastic APM 的 transaction／span 文件 → 跟 to_span 同一種格式。
    耗時是微秒（*.duration.us）；錯誤看 event.outcome；對象是 span.destination.service.resource（"oracle"、"mes-api:8080"）；
    span 本身沒有例外（例外是 logs-apm.error 的獨立文件，先不接）。attrs、resource 攤平成 OTel 的名字，根因分析的
    commonality（k8s.pod.name、db.statement）兩種來源同一套設定。"""
    event = get_field(src, "processor.event") or ("transaction" if get_field(src, "transaction.duration.us") is not None else "span")
    obj = (src.get(event) if isinstance(src.get(event), dict) else {k[len(event) + 1:]: v for k, v in src.items() if k.startswith(event + ".")}) or {}
    labels = src.get("labels") or {}
    us = get_field(src, "timestamp.us")
    attrs = {k: v for k, v in {
        "db.statement": get_field(obj, "db.statement"), "db.system": get_field(obj, "subtype") if (get_field(obj, "type") or "").startswith("db") else None,
        "http.request.method": get_field(src, "http.request.method"), "http.response.status_code": get_field(src, "http.response.status_code"),
        "url.path": get_field(src, "url.path"), f"{event}.type": get_field(obj, "type"), "span.subtype": get_field(obj, "subtype"),
        "transaction.result": get_field(obj, "result"),
        **{f"labels.{k}": v for k, v in labels.items()}}.items() if v is not None}
    resource = {k: v for k, v in {
        "service.name": get_field(src, "service.name"), "service.version": get_field(src, "service.version"),
        "service.environment": get_field(src, "service.environment"),
        "k8s.pod.name": get_field(src, "kubernetes.pod.name"), "k8s.namespace.name": get_field(src, "kubernetes.namespace"),
        "k8s.node.name": get_field(src, "kubernetes.node.name") or get_field(src, "host.name"), "host.name": get_field(src, "host.name"),
        "dept": labels.get("dept"), "group": labels.get("group"), "data_stream.namespace": get_field(src, "data_stream.namespace")}.items() if v is not None}
    return {"trace_id": get_field(src, "trace.id"), "span_id": get_field(obj, "id"), "parent": get_field(src, "parent.id") or None,
            "service": get_field(src, "service.name") or "?", "name": get_field(obj, "name") or "?",
            "kind": _apm_kind(event, obj), "target": get_field(obj, "destination.service.resource") or get_field(src, "service.target.name"),
            "start_ms": us / 1000.0 if isinstance(us, (int, float)) else start_ms(src["@timestamp"]),
            "dur_ms": duration_ms(get_field(obj, "duration.us")) * 1000.0,     # 微秒 → 毫秒（duration_ms 是奈秒 → 毫秒）
            "error": get_field(src, "event.outcome") == "failure", "exception": None,
            "links": [sid for l in (get_field(obj, "links") or []) if (sid := get_field(l, "span.id"))],
            "attrs": attrs, "resource": resource}


class ApmAdapter(SourceAdapter):
    name = "apm"
    trace_fields = _APM_FIELDS
    trace_sort = [{"@timestamp": "asc"}, {"_doc": "asc"}]
    trace_id_field = "trace.id"
    span_id_field = "span.id"
    aliases: list = []                   # 目前沒有（APM 的序列用 `keys:` 取短名；要讓 match 直接寫 service.name 等再加）

    def span_ids_filter(self, span_ids: list[str]) -> dict:
        # transaction 的 id 在 transaction.id，span 的在 span.id
        return {"bool": {"should": [{"terms": {"span.id": span_ids}}, {"terms": {"transaction.id": span_ids}}], "minimum_should_match": 1}}

    def can_handle(self, doc: dict) -> bool:
        return get_field(doc, "processor.event") in ("transaction", "span") or get_field(doc, "trace.id") is not None

    def owns_index(self, index: str) -> bool:
        return "apm" in index

    def to_span(self, doc: dict) -> dict:
        return _apm_span(doc)

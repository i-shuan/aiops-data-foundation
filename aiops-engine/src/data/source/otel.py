"""OTel：我們的 Collector 寫的 k8s-*、metrics-*.otel-*、traces-generic.otel-*（Collector 不在這個 repo，格式沿用）。

每個 span 一筆；欄位是 resource.attributes.*、attributes.* 的巢狀寫法（trace_id、span_id、parent_span_id）；耗時是奈秒；有 kind。
"""
from __future__ import annotations

from common.infra.es import get_field
from data.source.adapter import SourceAdapter, duration_ms, start_ms

_SPAN_FIELDS = ["@timestamp", "trace_id", "span_id", "parent_span_id", "name", "kind", "duration", "status.code", "links",
                "resource.attributes", "attributes"]


class OtelAdapter(SourceAdapter):
    name = "otel"
    trace_fields = _SPAN_FIELDS
    trace_sort = [{"@timestamp": "asc"}, {"span_id": "asc"}]
    trace_id_field = "trace_id"
    span_id_field = "span_id"
    # 同一個概念在不同序列的欄位名稱（log、metrics、trace 的鑰匙都是 OTel 路徑；Java 的 HTTP 指標由 Collector 改名）
    aliases = [
        ("service", "resource.attributes.service.name", "entry.service"),
        ("namespace", "k8s.namespace.name", "resource.attributes.k8s.namespace.name"),
        ("pod", "k8s.pod.name", "resource.attributes.k8s.pod.name"),
        ("deployment", "k8s.deployment.name", "resource.attributes.k8s.deployment.name"),
        ("route", "http.route", "attributes.http.route"),
        ("method", "http.request.method", "attributes.http.request.method"),
    ]

    def span_ids_filter(self, span_ids: list[str]) -> dict:
        return {"terms": {"span_id": span_ids}}

    def can_handle(self, doc: dict) -> bool:
        return "trace_id" in doc and "span_id" in doc          # OTel 的 span 一定有這兩個（to_span 直接取）；APM 的文件是 trace.id、span.id（巢狀）

    def owns_index(self, index: str) -> bool:
        return False        # Collector 的索引名由使用者設，沒有可靠的特徵；沒有別的來源認領時，用宣告的來源（見 for_index）

    def to_span(self, doc: dict) -> dict:
        attrs = doc.get("attributes") or {}
        # db.system.name：新版 DB 語意（舊的 db.system 改名）；沒有對象的 Client span 段名沒有「→」，N+1 規則看不到
        target = (get_field(attrs, "peer.service") or get_field(attrs, "db.system") or get_field(attrs, "db.system.name")
              or get_field(attrs, "server.address"))
        return {"trace_id": doc["trace_id"], "span_id": doc["span_id"], "parent": doc.get("parent_span_id") or None,
            "service": get_field(doc, "resource.attributes.service.name") or "?", "name": doc.get("name", "?"),
            "kind": doc.get("kind", ""), "target": target, "start_ms": start_ms(doc["@timestamp"]),
            "dur_ms": duration_ms(doc.get("duration")), "error": get_field(doc, "status.code") == "Error",
            "exception": get_field(attrs, "exception.type"),
            "links": [l["span_id"] for l in doc.get("links") or [] if l.get("span_id")],
            "attrs": attrs, "resource": get_field(doc, "resource.attributes") or {}}
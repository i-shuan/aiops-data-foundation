"""把一個 trace 的所有 span 合成一筆（traces-merged-<group>-<日期>，_id = trace_id）。

Collector 把每一段原樣寫進 traces-generic.otel-<group>（只留 2 天）；排程器每分鐘跑 `detector trace-merge`：
  1. 找這一段時間有新 span 進來的 trace（raw 文件的 event.ingested 由 pipeline traces-otel 蓋）
  2. 讀回這些 trace 全部的 span（含更早到的）
  3. 合成一筆 upsert：晚到的 span 讓那個 trace 再被合一次、整份覆蓋，所以合得完整；重跑不會重複
文件：上層是摘要（入口、總耗時、有沒有錯、經過哪些服務），`spans` 是每一段（nested，欄位跟 core.traces 的 span 一樣），
`entries` 是入口（Server、Consumer、沒有上一層的 span），讓「依服務／API 挑請求」不用展開 spans。
docs/pdca/phase1-collect/2026-10-trace-merge.md。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiops.core.connection import ES, time_range
from aiops.core.entity.trace import group_traces, root_of, trace_id_field
from aiops.core.repository.base import as_backend

STATE_INDEX = "aiops-state"
STATE_ID = "trace-merge"
STATE_MAPPING = {"mappings": {"dynamic": False, "properties": {"last": {"type": "date"}, "updated_at": {"type": "date"}}}}
MERGED_PREFIX = "traces-merged"


def is_entry(s: dict, ids: set[str]) -> bool:
    """入口：HTTP 請求（Server）、訊息消費者（Consumer）、沒有上一層的 span（.NET 自動埋點的 Kafka 消費者查 DB 那段自成一個 trace）。"""
    return s["kind"] in ("Server", "Consumer") or not s.get("parent") or s["parent"] not in ids


def build(spans: list[dict], merged_at: datetime, prefix: str = MERGED_PREFIX) -> tuple[str, dict]:
    """同一個 trace 的 span（core.traces 的格式）→ (index 名稱, 文件)。index 依 group 與根 span 的日期（UTC）分。
    prefix：index 名的前綴；工廠的履歷用 lot-paths（plugins/factory/paths.py），文件格式一樣。"""
    root = root_of(spans)
    ids = {s["span_id"] for s in spans}
    start = datetime.fromtimestamp(root["start_ms"] / 1000, timezone.utc)
    end_ms = max(s["start_ms"] + s["dur_ms"] for s in spans)
    res = root.get("resource") or {}
    group = res.get("group") or res.get("data_stream.namespace") or "unassigned"
    doc = {
        "@timestamp": start.isoformat(), "trace_id": root["trace_id"], "group": group, "dept": res.get("dept"),
        "root": {"service": root["service"], "name": root["name"], "kind": root["kind"], "error": bool(root["error"])},
        "duration_ms": round(max(root["dur_ms"], end_ms - root["start_ms"]), 3),
        "error": any(s["error"] for s in spans),
        "services": sorted({s["service"] for s in spans}),
        "span_count": len(spans),
        "merged_at": merged_at.isoformat(),
        "entries": [{"span_id": s["span_id"], "service": s["service"], "name": s["name"], "kind": s["kind"],
                     "start_ms": s["start_ms"], "duration_ms": s["dur_ms"], "error": bool(s["error"])}
                    for s in spans if is_entry(s, ids)],
        "spans": [{"span_id": s["span_id"], "parent": s.get("parent"), "service": s["service"], "name": s["name"], "kind": s["kind"],
                   "target": s.get("target"), "start_ms": s["start_ms"], "dur_ms": s["dur_ms"], "error": bool(s["error"]),
                   "exception": s.get("exception"), "links": s.get("links") or [], "attrs": s.get("attrs") or {},
                   "resource": s.get("resource") or {}} for s in sorted(spans, key=lambda x: (x["start_ms"], x["span_id"]))],
    }
    return f"{prefix}-{group}-{start:%Y.%m.%d}", doc


def updated_trace_ids(es: ES, raw_index: str, since: datetime, until: datetime) -> list[str]:
    """這段時間（依 event.ingested）有 span 進來的 trace。event.ingested 我們的 pipeline traces-otel 蓋、APM 的 Fleet pipeline 也蓋。"""
    out, after = [], None
    body = {"size": 0, "query": {"range": {"event.ingested": {"gte": since.isoformat(), "lt": until.isoformat()}}},
            "aggs": {"t": {"composite": {"size": 2000, "aiops.core.repository": [{"id": {"terms": {"field": trace_id_field(raw_index)}}}]}}}}
    while True:
        if after:
            body["aggs"]["t"]["composite"]["after"] = after
        agg = es.search(raw_index, body).get("aggregations", {}).get("t", {})
        out += [b["key"]["id"] for b in agg.get("buckets", [])]
        after = agg.get("after_key")
        if not after:
            return out


def merge(es: ES, cfg: dict, since: datetime, until: datetime, now: datetime | None = None) -> int:
    """[since, until) 有新 span 的 trace 全部重新合成、寫進 merged。回傳合了幾個 trace。
    span 的時間用 until 往前 2 天（raw 的保留天數）到 until 往後 5 分鐘（時鐘偏）來框。"""
    now = now or datetime.now(timezone.utc)
    raw = cfg.get("raw_index", "traces-generic.otel-*")
    ids = updated_trace_ids(es, raw, since, until)
    if not ids:
        return 0
    spans = as_backend(es).spans(raw, ids, until - timedelta(days=2), until + timedelta(minutes=5))
    docs: dict[str, list[tuple[str, dict]]] = {}
    for tid, tspans in group_traces(spans).items():
        index, doc = build(tspans, now)
        docs.setdefault(index, []).append((tid, doc))
    for index, batch in docs.items():
        es.bulk_index(index, batch)
    return sum(len(b) for b in docs.values())


def load_state(es: ES) -> datetime | None:
    got = es.req("GET", f"/{STATE_INDEX}/_doc/{STATE_ID}", ok=(200, 404))
    last = (got.get("_source") or {}).get("last") if got.get("found") else None
    return datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None


def save_state(es: ES, last: datetime, now: datetime) -> None:
    es.ensure_index(STATE_INDEX, STATE_MAPPING)
    es.req("PUT", f"/{STATE_INDEX}/_doc/{STATE_ID}?refresh=true", {"last": last.isoformat(), "updated_at": now.isoformat()})


def run(es: ES, cfg: dict, at: datetime, window: timedelta, chunk: timedelta = timedelta(minutes=10)) -> tuple[datetime, datetime, int]:
    """從上次合到的時間（沒有就 at − window）合到 at − 10 秒（剛寫進 ES 的要等 refresh）；中斷很久的話分段補。
    回傳 (since, until, 合了幾個)。"""
    mcfg = cfg.get("merge", {})
    until = at - timedelta(seconds=int(mcfg.get("settle_seconds", 10)))
    last = load_state(es)
    floor = at - timedelta(days=2)                      # raw 只留 2 天，更早的補不到
    since = max(last or (at - window), floor)
    if since >= until:
        return since, until, 0
    n, t = 0, since
    while t < until:
        t2 = min(t + chunk, until)
        n += merge(es, cfg, t, t2, at)
        t = t2
    save_state(es, until, at)
    return since, until, n


def time_filter(start: datetime, end: datetime) -> dict:   # 給測試與讀端用
    return time_range(start, end)

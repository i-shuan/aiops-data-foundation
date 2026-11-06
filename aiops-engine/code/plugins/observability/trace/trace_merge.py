"""把一個 trace 的所有 span 合成一筆（traces-merged-<group>-<日期>，_id = trace_id）。

Collector 把每一段原樣寫進 traces-generic.otel-<group>（只留 2 天）；排程器每分鐘跑 `detector trace-merge`：
  1. 找這一段時間有新 span 進來的 trace（raw 文件的 event.ingested 由 pipeline traces-otel 蓋）
  2. 讀回這些 trace 全部的 span（含更早到的）
  3. 合成一筆 upsert：晚到的 span 讓那個 trace 再被合一次、整份覆蓋，所以合得完整；重跑不會重複
文件格式在 sources.traces.merged_doc（上層是摘要、`spans` 每一段、`entries` 入口），工廠的履歷（plugins/factory/paths）用同一個函式。
docs/pdca/phase1-collect/2026-10-trace-merge.md。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.es_client import ES, time_range
from sources import spans_of
from sources.traces import group_traces, merged_doc, trace_id_field

STATE_INDEX = "aiops-state"
STATE_ID = "trace-merge"
STATE_MAPPING = {"mappings": {"dynamic": False, "properties": {"last": {"type": "date"}, "updated_at": {"type": "date"}}}}
MERGED_PREFIX = "traces-merged"


def updated_trace_ids(es: ES, raw_index: str, since: datetime, until: datetime) -> list[str]:
    """這段時間（依 event.ingested）有 span 進來的 trace。event.ingested 我們的 pipeline traces-otel 蓋、APM 的 Fleet pipeline 也蓋。"""
    out, after = [], None
    body = {"size": 0, "query": {"range": {"event.ingested": {"gte": since.isoformat(), "lt": until.isoformat()}}},
            "aggs": {"t": {"composite": {"size": 2000, "sources": [{"id": {"terms": {"field": trace_id_field(raw_index)}}}]}}}}
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
    spans = spans_of(es, raw, ids, until - timedelta(days=2), until + timedelta(minutes=5))
    docs: dict[str, list[tuple[str, dict]]] = {}
    for tid, tspans in group_traces(spans).items():
        index, doc = merged_doc(tspans, now, MERGED_PREFIX)
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

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

from shared.infra.es import time_range
from shared.config.trace import TraceConfig
from detect.store.models import Trace

STATE_ID = "trace-merge"


def merge(repo, trace_config: TraceConfig, since: datetime, until: datetime, now: datetime | None = None) -> int:
    """[since, until) 有新 span 的 trace 全部重新合成、寫進 merged。回傳合了幾個 trace。
    span 的時間用 until 往前 2 天（raw 的保留天數）到 until 往後 5 分鐘（時鐘偏）來框。"""
    now = now or datetime.now(timezone.utc)
    raw = trace_config.raw_index
    ids = repo.traces.updated_trace_ids(raw, since, until)
    if not ids:
        return 0
    spans = repo.traces.spans(raw, ids, until - timedelta(days=2), until + timedelta(minutes=5))
    docs: dict[str, list[tuple[str, dict]]] = {}
    for tid, t in Trace.group(spans).items():
        index, doc = t.to_merged_doc(now)
        docs.setdefault(index, []).append((tid, doc))
    return repo.traces.write_merged(docs)


def load_state(repo) -> datetime | None:
    got = repo.state.get(STATE_ID)
    last = (got or {}).get("last")
    return datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None


def save_state(repo, last: datetime, now: datetime) -> None:
    repo.state.set(STATE_ID, {"last": last.isoformat(), "updated_at": now.isoformat()})


def run(repo, trace_config: TraceConfig, at: datetime, window: timedelta, chunk: timedelta = timedelta(minutes=10)) -> tuple[datetime, datetime, int]:
    """從上次合到的時間（沒有就 at − window）合到 at − 10 秒（剛寫進 ES 的要等 refresh）；中斷很久的話分段補。
    回傳 (since, until, 合了幾個)。"""
    until = at - timedelta(seconds=int(trace_config.merge.settle_seconds))
    last = load_state(repo)
    floor = at - timedelta(days=2)                      # raw 只留 2 天，更早的補不到
    since = max(last or (at - window), floor)
    if since >= until:
        return since, until, 0
    n, t = 0, since
    while t < until:
        t2 = min(t + chunk, until)
        n += merge(repo, trace_config, t, t2, at)
        t = t2
    save_state(repo, until, at)
    return since, until, n


def time_filter(start: datetime, end: datetime) -> dict:   # 給測試與讀端用
    return time_range(start, end)

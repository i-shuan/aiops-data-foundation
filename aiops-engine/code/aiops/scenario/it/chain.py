"""IT 的併件依據：trace 裡「誰呼叫誰」（下游出事連帶上游；investigator/alerts.merge 的 calls）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiops.core.entity.trace as T
import aiops.service.rca.analyze as A


def call_edges(repo, cfg: dict, services: set[str], start: datetime, end: datetime, per: int = 100) -> set[tuple[str, str]]:
    """這段時間 trace 裡這幾個服務之間誰呼叫誰：{(上游, 下游)}。
    每個服務取最近 per 個收到的請求、整個 trace 讀回來（含其他服務的 span），用根因分析同一套 topology 算邊
    （父 span 的服務 ≠ 子 span 的服務 → 父呼叫子）；只留兩端都在 services 裡的。少於 2 個服務、沒有 trace 都回空集合。"""
    if len(services) < 2:
        return set()
    ix, pad = cfg["indices"]["traces"], timedelta(minutes=5)
    ids: list[str] = []
    for s in sorted(services):
        ids += repo.traces.service_trace_ids(ix, s, start, end, per)
    ids = list(dict.fromkeys(ids))
    if not ids:
        return set()
    edges = A.topology(T.group_traces(repo.traces.spans(ix, ids, start - pad, end + pad)))
    return {(e.caller, e.callee) for e in edges if e.caller in services and e.callee in services and e.caller != e.callee}

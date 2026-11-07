"""IT 的併件依據：trace 裡「誰呼叫誰」（下游出事連帶上游；incident/alert_grouper 的 calls）。"""
from __future__ import annotations

from incident.config.rca import RcaConfig
from datetime import datetime, timedelta

import data.store.models as T
from detect.store.models import entity_of
from common.scenario.base import Chain


def call_edges(repo, rca_config: RcaConfig, services: set[str], start: datetime, end: datetime, per: int = 100) -> set[tuple[str, str]]:
    """這段時間 trace 裡這幾個服務之間誰呼叫誰：{(上游, 下游)}。
    每個服務取最近 per 個收到的請求、整個 trace 讀回來（含其他服務的 span），用根因分析同一套 topology 算邊
    （父 span 的服務 ≠ 子 span 的服務 → 父呼叫子）；只留兩端都在 services 裡的。少於 2 個服務、沒有 trace 都回空集合。"""
    if len(services) < 2:
        return set()
    ix, pad = rca_config.indices.traces, timedelta(minutes=5)
    ids: list[str] = []
    for s in sorted(services):
        ids += repo.traces.service_trace_ids(ix, s, start, end, per)
    ids = list(dict.fromkeys(ids))
    if not ids:
        return set()
    edges = T.Trace.edges(T.Trace.group(repo.traces.spans(ix, ids, start - pad, end + pad)))
    return {(e.caller, e.callee) for e in edges if e.caller in services and e.callee in services and e.caller != e.callee}


class CallEdges:
    """跨服務（一層）：這段時間 trace 裡「誰呼叫誰」，只看有件開著、或這次有異常的服務。rca_config 的 alerts.chain_sample 是每個服務抽幾個請求。"""

    def __call__(self, repo, rca_config: RcaConfig, anomalies: list[dict], open_alerts: list, start: datetime, now: datetime) -> Chain:
        keys = repo.scenario.entity_keys
        svcs = {al.service for al in open_alerts if al.category == "service"}
        svcs |= {ent[1] for ent in (entity_of(a, keys) for a in anomalies) if ent[0] == "service"}
        return Chain(calls=call_edges(repo, rca_config, svcs, start, now, rca_config.alerts.chain_sample))

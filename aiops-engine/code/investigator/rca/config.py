"""rca.yaml 的設定模型（根因分析、事件整合、相似事故都讀這一份）。"""
from __future__ import annotations

from typing import Any

from core.config import Dur, NonNeg, Num, PosInt, Ratio, Strict


class Indices(Strict):
    anomalies: str
    traces: str | None = None          # metrics_only 沒有履歷
    logs: str | None = None


class Trend(Strict):
    windows: PosInt | None = None
    window: Dur | None = None


class Commonality(Strict):
    resource: list[str] | None = None
    attrs: list[str] | None = None
    min_share: Ratio | None = None
    min_lift: NonNeg | None = None
    min_traces: PosInt | None = None


class Changes(Strict):
    lookback: Dur | None = None
    query: dict[str, Any] | None = None


class SeverityClass(Strict):
    name: str
    patterns: list[str] | None = None


class SeverityCfg(Strict):
    """investigator/alerts/severity.GradeOptions(**severity)（classes 先 pop 掉）。"""
    window: PosInt | None = None
    min_coverage: Ratio | None = None
    min_duration: NonNeg | None = None
    min_span_critical: NonNeg | None = None
    min_points_per_class: PosInt | None = None
    deploy_grace: NonNeg | None = None
    classes: dict[str, list[str] | str] | list[SeverityClass] | None = None


class Alerts(Strict):
    """事件整合（investigator/alerts/command.py）＋ plugin 讀的換版、前後站設定（plugins/*/__init__.py）。"""
    close_after: Dur | None = None
    lookback: Dur | None = None
    severity: SeverityCfg | None = None
    critical_levels: list[str] | None = None
    deploy_grace: bool | None = None
    deploy_index: str | None = None
    version_field: str | None = None
    deploy_entity_field: str | None = None
    deploy_time_field: str | None = None
    deploy_filter: dict[str, Any] | None = None
    rca: bool | None = None
    rca_lead: Dur | None = None
    rca_retry: bool | None = None
    rca_retry_after: Dur | None = None
    rca_retry_max: PosInt | None = None
    similar: bool | None = None
    similar_weights: dict[str, Num] | None = None
    llm: bool | None = None
    llm_provider: str | None = None
    chain: bool | None = None
    chain_sample: PosInt | None = None
    paths_lookback: Dur | None = None
    feedback_url: str | None = None
    report_url: str | None = None


class IncidentsDb(Strict):
    url: str | None = None
    sql: str | None = None


class RcaConfig(Strict):
    indices: Indices | None = None
    timezone: str | None = None
    compare_with: Dur | None = None
    ref_gap: Dur | None = None
    sample: PosInt | None = None
    interval: Dur | None = None
    entry_lag: Dur | None = None
    trend: Trend | None = None
    commonality: Commonality | None = None
    changes: Changes | None = None
    alerts: Alerts | None = None
    incidents_db: IncidentsDb | None = None

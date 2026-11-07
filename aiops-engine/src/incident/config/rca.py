"""rca.yaml：根因分析、事件整合、通知。"""
from __future__ import annotations

from typing import Any

from common.config.common import Dur, NonNeg, Num, PosInt, Ratio, Strict, Tz


# ── rca.yaml ──

class Indices(Strict):
    anomalies: str
    traces: str | None = None          # metrics_only 沒有履歷
    logs: str | None = None


class Trend(Strict):
    windows: PosInt = 6                      # 往前看幾個時間窗
    window: Dur = "10m"


class Commonality(Strict):
    resource: list[str] = ["k8s.pod.name", "service.version", "k8s.node.name"]   # 慢的請求共同的資源屬性
    attrs: list[str] = ["lot.id", "db.statement"]                              # 慢的請求共同的 span 屬性
    min_share: Ratio = 0.6
    min_lift: NonNeg = 2
    min_traces: PosInt = 5


class Changes(Strict):
    lookback: Dur = "12h"
    query: dict[str, Any] | None = None


class SeverityClass(Strict):
    name: str
    patterns: list[str] | None = None


class SeverityCfg(Strict):
    """investigator/severity.GradeOptions(**severity)（classes 先 pop 掉）。"""
    window: PosInt | None = None
    min_coverage: Ratio | None = None
    min_duration: NonNeg | None = None
    min_span_critical: NonNeg | None = None
    min_points_per_class: PosInt | None = None
    deploy_grace: NonNeg | None = None
    classes: dict[str, list[str] | str] | list[SeverityClass] | None = None


class Alerts(Strict):
    """事件整合（incident/alert_service.py）＋ 情境策略讀的換版、前後站設定（scenario/it、scenario/factory）。"""
    close_after: Dur = "30m"                # 這麼久沒有新異常就結案
    lookback: Dur = "2h"
    severity: SeverityCfg | None = None
    critical_levels: list[str] | None = None    # 沒寫用情境的（Scenario.critical_levels）
    deploy_grace: bool = True
    deploy_index: str | None = None
    version_field: str | None = None
    deploy_entity_field: str | None = None
    deploy_time_field: str | None = None
    deploy_filter: dict[str, Any] | None = None
    rca: bool = True
    rca_lead: Dur = "0s"                     # 根因分析的時間範圍從事故開始往前多少
    rca_retry: bool = True
    rca_retry_after: Dur = "2m"
    rca_retry_max: PosInt = 5
    similar: bool = True
    similar_weights: dict[str, Num] | None = None
    llm: bool = True
    llm_provider: str | None = None          # 沒寫用環境變數 LLM_PROVIDER（settings.llm_provider）
    chain: bool = True
    chain_sample: PosInt = 100
    paths_lookback: Dur = "3d"
    feedback_url: str | None = None
    report_url: str | None = None


class IncidentsDb(Strict):
    url: str | None = None
    sql: str | None = None


class RcaConfig(Strict):
    indices: Indices                         # 一定要有（anomalies；traces、logs 看情境）
    timezone: Tz = "Asia/Taipei"
    compare_with: Dur = "7d"                 # 跟多久以前的同一段比
    ref_gap: Dur = "10m"
    sample: PosInt = 200
    interval: Dur = "5m"
    entry_lag: Dur = "0s"                    # 入口比文件時間晚最多多久（工廠履歷：lot 開始到到這一站）
    trend: Trend = Trend()
    commonality: Commonality = Commonality()
    changes: Changes | None = None           # 沒寫 query 就不出「事故前的變更」那一段
    alerts: Alerts = Alerts()
    incidents_db: IncidentsDb | None = None

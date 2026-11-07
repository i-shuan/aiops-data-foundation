"""trace.yaml：trace 的合成、每一段的 baseline 與規則。"""
from __future__ import annotations

from shared.config.common import Detect, Direction, Dur, NonNeg, PosInt, Ratio, Status, Strict, Train


# ── trace.yaml ──

class Merge(Strict):
    settle_seconds: NonNeg = 10              # 等這麼久才合（apm-server 批次送、ES refresh；APM 要比 Collector 久）


class TraceRules(Strict):
    repeated_calls: NonNeg | None = None
    swallowed_rate: Ratio | None = None


class MetricRule(Strict):
    direction: Direction = "both"
    min_abs: NonNeg = 0.0


class TraceMetrics(Strict):
    """detector/trace.METRICS 四種數字各自怎麼判。"""
    calls: MetricRule = MetricRule()
    time_ms: MetricRule = MetricRule()
    error_rate: MetricRule = MetricRule()
    swallowed_rate: MetricRule = MetricRule()


class TraceConfig(Strict):
    traces_index: str                        # 合成後的索引（traces-merged-*）
    interval: Dur = "5m"
    history_weeks: PosInt = 4
    sample_per_bucket: PosInt = 100
    min_requests: NonNeg = 5
    clock_skew_ms: NonNeg = 500
    sample_policy: str | None = None
    raw_index: str = "traces-generic.otel-*"   # 合成前的原始 span（APM 是 traces-apm-*）
    merge: Merge = Merge()
    rules: TraceRules = TraceRules()
    train: Train = Train()
    detect: Detect = Detect()
    status: Status | None = None             # 沒寫的鍵不算有（with_status 用 .get）
    metrics: TraceMetrics = TraceMetrics()

"""trace.yaml：trace 的合成、每一段的 baseline 與規則。"""
from __future__ import annotations

from shared.config.common import Detect, Direction, Dur, NonNeg, PosInt, Ratio, Status, Strict, Train


# ── trace.yaml ──

class Merge(Strict):
    settle_seconds: NonNeg | None = None


class TraceRules(Strict):
    repeated_calls: NonNeg | None = None
    swallowed_rate: Ratio | None = None


class MetricRule(Strict):
    direction: Direction | None = None
    min_abs: NonNeg | None = None


class TraceMetrics(Strict):
    """detector/trace.METRICS 四種數字各自怎麼判。"""
    calls: MetricRule | None = None
    time_ms: MetricRule | None = None
    error_rate: MetricRule | None = None
    swallowed_rate: MetricRule | None = None


class TraceConfig(Strict):
    traces_index: str | None = None
    interval: Dur | None = None
    history_weeks: PosInt | None = None
    sample_per_bucket: PosInt | None = None
    min_requests: NonNeg | None = None
    clock_skew_ms: NonNeg | None = None
    sample_policy: str | None = None
    raw_index: str | None = None
    merge: Merge | None = None
    rules: TraceRules | None = None
    train: Train | None = None
    detect: Detect | None = None
    status: Status | None = None
    metrics: TraceMetrics | None = None

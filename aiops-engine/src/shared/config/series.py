"""series.yaml：序列（metrics 的 baseline 與偵測）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import model_validator

from shared.config.adjustments import Adjustment
from shared.config.common import _same_length, Detect, Direction, Dur, NonNeg, Num, PosInt, Query, Ratio, Status, Strict, Train, Value


# ── series.yaml ──

class AdaptiveMinEvents(Strict):
    rate: NonNeg | None = None
    count: NonNeg | None = None
    gauge: NonNeg | None = None


class Adaptive(Strict):
    resolutions: list[Dur] | None = None
    min_events: AdaptiveMinEvents | None = None
    coverage: Ratio | None = None


class Group(Strict):
    name: str
    match: dict[str, Any]


class Borrowing(Strict):
    min_members: PosInt | None = None
    z: NonNeg | None = None
    resolution: Dur | None = None


class Shift(Strict):
    """detector/cmd_shift.DEFAULTS。"""
    window: Dur | None = None
    min_points: PosInt | None = None
    min_share: Ratio | None = None
    max_spread: NonNeg | None = None


class Series(Strict):
    """一條序列（detector/es.py 的查詢、cmd_baseline 的判斷）。"""
    name: str
    index: str
    by: list[str]
    value: Value
    keys: list[str] | None = None
    filter: Query | None = None
    time_field: str | None = None
    scale: NonNeg | None = None
    events: Literal["value"] | dict[str, Any] | None = None
    kind: Literal["rate", "count", "gauge"] | None = None
    fill_zero: bool | None = None
    direction: Direction | None = None
    min_abs: NonNeg | None = None
    min_count: NonNeg | None = None
    confirm_points: PosInt | None = None
    instant_z: NonNeg | None = None
    borrow: bool | None = None
    resolution: Dur | None = None
    min_resolution: Dur | None = None
    buckets: list[Num] | None = None

    @model_validator(mode="after")
    def _check(self):
        _same_length(self.keys, self.by)
        return self


class SeriesConfig(Strict):
    interval: Dur | None = None
    history_weeks: PosInt | None = None
    fine_history: Dur | None = None
    lag: Dur | None = None
    timezone: str | None = None
    adaptive: Adaptive | None = None
    status: Status | None = None
    overrides: list[Adjustment] | None = None
    groups: list[Group] | None = None
    borrowing: Borrowing | None = None
    train: Train | None = None
    detect: Detect | None = None
    shift: Shift | None = None
    adjustments_file: str | None = None
    calendar_file: str | None = None
    series: list[Series] | None = None

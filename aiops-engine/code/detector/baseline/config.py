"""series.yaml 的設定模型（train、detect、tune、shift、backtest 都讀這一份）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import model_validator

from core.config import Dur, Direction, NonNeg, Num, PosInt, Query, Ratio, Strict, Value, same_length
from detector.adjust.config import Adjustment
from sources.config import Source, Store


class Train(Strict):
    """detector/baseline/model.TrainOptions(**cfg["train"])：欄位要跟 dataclass 一樣。"""
    timezone: str | None = None
    min_weeks: PosInt | None = None
    min_days: PosInt | None = None
    min_points: PosInt | None = None
    min_slot_points: PosInt | None = None


class Detect(Strict):
    """detector/baseline/model.DetectOptions(**cfg["detect"])。"""
    z: NonNeg | None = None
    rel_floor: NonNeg | None = None
    abs_floor: NonNeg | None = None
    alpha: Ratio | None = None


class Status(Strict):
    """跳一次是 warning，連續幾窗才是 anomaly（detector/baseline/detect.with_status）。"""
    confirm_points: PosInt | None = None
    confirm_span: Dur | None = None
    instant_z: NonNeg | None = None



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
    """detector/adjust/shift.DEFAULTS。"""
    window: Dur | None = None
    min_points: PosInt | None = None
    min_share: Ratio | None = None
    max_spread: NonNeg | None = None


class Series(Strict):
    """一條序列（sources 的查詢、detector/baseline/detect.py 的判斷）。"""
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
        same_length(self.keys, self.by)
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
    source: Source | None = None
    store: Store | None = None
    series: list[Series] | None = None

"""異常分析（detect/）幾種設定共用的小段：序列與 trace 都有的 train／detect／status，行事曆與慢性病共用的外部資料來源。"""
from __future__ import annotations

from typing import Any, Literal

from common.config.common import Dur, NonNeg, PosInt, Ratio, Strict

Value = Literal["count"] | dict[str, Any]      # count 或 {sum|avg|max|min|percentile|ratio|hist_count|hist_ratio|hist_ratio_above|div: …}


class Train(Strict):
    """dynamic_baseline.TrainOptions(**series_config.train.model_dump(exclude_none=True))：欄位要跟 dataclass 一樣。"""
    timezone: str | None = None
    min_weeks: PosInt | None = None
    min_days: PosInt | None = None
    min_points: PosInt | None = None
    min_slot_points: PosInt | None = None


class Detect(Strict):
    """dynamic_baseline.DetectOptions(**…detect.model_dump(exclude_none=True))。"""
    z: NonNeg | None = None
    rel_floor: NonNeg | None = None
    abs_floor: NonNeg | None = None
    alpha: Ratio | None = None


class Status(Strict):
    """跳一次是 warning，連續幾窗才是 anomaly（detector/cmd_baseline.with_status）。"""
    confirm_points: PosInt | None = None
    confirm_span: Dur | None = None
    instant_z: NonNeg | None = None


class Provider(Strict):
    """外部資料來源（detector/sources.py）：行事曆的 sources、慢性病的 owners_source。match／series／detect 只有行事曆用。"""
    provider: Literal["file", "http", "taiwan"]
    path: str | None = None
    url: str | None = None
    years: list[int] | None = None
    enabled: bool | None = None
    cache_dir: str | None = None
    cache_ttl: Dur | None = None
    headers: dict[str, str] | None = None
    timeout: NonNeg | None = None
    match: dict[str, Any] | None = None
    series: str | None = None
    detect: bool | None = None

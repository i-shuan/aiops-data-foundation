"""異常分析（detect/）幾種設定共用的小段：序列與 trace 都有的 train／detect／status，行事曆與慢性病共用的外部資料來源。"""
from __future__ import annotations

from typing import Any, Literal

from common.config.common import Dur, NonNeg, PosInt, Ratio, Strict, Tz

Value = Literal["count"] | dict[str, Any]      # count 或 {sum|avg|max|min|percentile|ratio|hist_count|hist_ratio|hist_ratio_above|div: …}


class Train(Strict):
    """dynamic_baseline.TrainOptions(**series_config.train.model_dump(exclude_none=True))：欄位要跟 dataclass 一樣。"""
    # 預設值跟 dynamic_baseline.TrainOptions 一樣（tests/test_config_defaults.py 鎖著）
    timezone: Tz = "Asia/Taipei"   # 週期依工廠當地時間（換班、上下班）
    min_weeks: PosInt = 2   # 「同星期幾 × 同小時」至少要有 2 天，不夠就退到「每天同小時」
    min_days: PosInt = 3   # 「每天同小時」至少要有 3 天，不夠就退到「不分時段」
    min_points: PosInt = 50
    min_slot_points: PosInt = 6


class Detect(Strict):
    """dynamic_baseline.DetectOptions(**…detect.model_dump(exclude_none=True))。"""
    # 預設值跟 dynamic_baseline.DetectOptions 一樣（tests/test_config_defaults.py 鎖著）
    z: NonNeg = 5.0   # 離平常 5 倍穩健標準差才算異常
    rel_floor: NonNeg = 0.05   # 平常幾乎不動的序列，波動至少當成平常值的 5%
    abs_floor: NonNeg = 1e-9
    alpha: Ratio = 0.001


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

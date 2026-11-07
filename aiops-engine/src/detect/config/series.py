"""series.yaml：序列（metrics 的 baseline 與偵測）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from detect.config.adjustments import Adjustment
from common.config.common import _same_length, Direction, Dur, NonNeg, Num, PosInt, Query, Ratio, Strict, Tz
from detect.config.types import Detect, Status, Train, Value


# ── series.yaml ──

class AdaptiveMinEvents(Strict):
    rate: NonNeg = 20   # 比例、百分位類每窗至少 20 個請求（比例、百分位：每個時間窗至少幾個事件才判斷）
    count: NonNeg = 10   # 筆數類每窗至少 10 筆
    gauge: NonNeg = 10


class Adaptive(Strict):
    resolutions: list[Dur] | None = None     # 沒寫用 dynamic_baseline.RESOLUTIONS
    min_events: AdaptiveMinEvents = AdaptiveMinEvents()
    coverage: Ratio = 0.3   # 至少三成的時間窗夠（只有白天有人用的 API 也算）


class Group(Strict):
    name: str
    match: dict[str, Any]


class Borrowing(Strict):
    min_members: PosInt = 3   # 組裡至少幾個成員有自己的 baseline，才算得出組 baseline
    z: NonNeg = 7.0   # 借來的門檻調高
    resolution: Dur | None = None            # 沒寫用 adaptive 最粗的那個


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
    key_names: list[str] | None = Field(default=None, alias="keys")   # yaml 的鍵叫 keys；屬性叫 key_names（不跟 dict 的 keys() 混）
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
        _same_length(self.key_names, self.by)
        return self


class SeriesConfig(Strict):
    interval: Dur = "5m"
    history_weeks: PosInt = 4   # 學過去幾週
    fine_history: Dur = "14d"   # 5 分鐘以下的粒度只學最近這麼久（30 秒一點 4 週是 8 萬點）（5 分鐘以下的粒度只學最近這麼久）
    lag: Dur = "0s"                          # 資料寫進來的延遲，最近這麼久的不判斷
    timezone: Tz | None = None
    adaptive: Adaptive | None = None         # 沒寫＝全部用 interval 這一個粒度
    status: Status | None = None             # 沒寫的鍵不算有（status_params 用 .get）
    overrides: list[Adjustment] = []
    groups: list[Group] = []
    borrowing: Borrowing = Borrowing()
    train: Train = Train()
    detect: Detect = Detect()
    shift: Shift | None = None
    adjustments_file: str | None = None      # 寫 null 是「不載入」，所以載入端用 .get（manual_adjust.load）
    calendar_file: str | None = None
    series: list[Series] = []

    @model_validator(mode="after")
    def _unique_names(self):
        """序列的 name 不能重複：異常、基準線、調整都用名字認序列，重複會悄悄互相蓋掉（OTel 與 APM 混寫同一份時最容易撞名，加來源前綴）。"""
        seen, dup = set(), []
        for s in self.series:
            dup.append(s.name) if s.name in seen else seen.add(s.name)
        if dup:
            raise ValueError(f"序列的 name 重複：{'、'.join(dict.fromkeys(dup))}（OTel 與 APM 混寫時兩邊同名的指標加來源前綴，例如 otel.http.requests）")
        return self


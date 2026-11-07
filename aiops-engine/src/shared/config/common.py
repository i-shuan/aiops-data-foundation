"""共用的型別與小段（所有設定種類都會用到）。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


# ── 共用型別 ──
Num = int | float


NonNeg = Annotated[int | float, Field(ge=0)]


Ratio = Annotated[int | float, Field(ge=0, le=1)]


PosInt = Annotated[int, Field(ge=1)]


# 時間長度：30s、1m、5m、15m、1h、14d、90s；pandas 的寫法（10min、2h30m 不收）
Dur = Annotated[str, StringConstraints(pattern=r"^\d+(\.\d+)?(ms|s|m|h|d|w)$")]


When = str | datetime | date                 # yaml 的 2026-10-10 解成 date、2026-10-10T02:00 解成 datetime、帶時區的是字串


Query = dict[str, Any] | list[dict[str, Any]]  # ES 的 query 片段；規則的 filter 可以是清單


Value = Literal["count"] | dict[str, Any]      # count 或 {sum|avg|max|min|percentile|ratio|hist_count|hist_ratio|hist_ratio_above|div: …}


Direction = Literal["up", "down", "both"]


Severity = Literal["warning", "high", "critical"]


Show = Literal["pct", "seconds", "hours", "ns_as_ms", "number", "value", "cores", "bytes"]


Op = Literal[">=", ">", "<=", "<", "==", "!="]


class Strict(BaseModel):
    """多出來的鍵就是錯（打錯字要在啟動時擋下）；欄位名跟 yaml 的鍵一樣，少數 Python 關鍵字用 alias（for、from）。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _same_length(keys, by, what: str = "keys") -> None:
    if keys is not None and by is not None and len(keys) != len(by):
        raise ValueError(f"{what} 有 {len(keys)} 個、by 有 {len(by)} 個，要一樣多（{what} 是 by 的每個欄位各取一個名字）")


# ── 共用的小段 ──

class Train(Strict):
    """detector/model.TrainOptions(**cfg["train"])：欄位要跟 dataclass 一樣。"""
    timezone: str | None = None
    min_weeks: PosInt | None = None
    min_days: PosInt | None = None
    min_points: PosInt | None = None
    min_slot_points: PosInt | None = None


class Detect(Strict):
    """detector/model.DetectOptions(**cfg["detect"])。"""
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

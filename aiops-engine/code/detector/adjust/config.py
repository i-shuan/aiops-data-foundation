"""調整的設定模型：一筆調整（series.yaml 的 overrides、adjustments.yaml）、行事曆 calendar.yaml。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import model_validator

from core.config import Dur, NonNeg, PosInt, Strict, When
from sources.config import Provider


class RecurringSchedule(Strict):
    cron: str | None = None
    duration: Dur | None = None
    days_of_month: list[int] | None = None


class RecurringEffect(Strict):
    z: NonNeg | None = None
    min_abs: NonNeg | None = None
    mute: bool | None = None
    train: Literal["keep", "exclude"] | None = None


class Adjustment(Strict):
    """一筆調整（detector/adjust/adjust.py）：series.yaml 的 overrides、adjustments.yaml 的 adjustments 同一種。kind 各自用到的欄位不同，
    這裡全部列出；哪一種缺什麼由 adjust.problem 判（它也要擋 ES 裡的壞資料）。"""
    id: str | None = None
    kind: Literal["override", "exclude_window", "reset_point", "recurring_override", "rule_suggestion"] | None = None
    series: str | None = None
    match: dict[str, Any] | None = None
    status: str | None = None               # active（預設）| proposed
    source: str | None = None               # manual | feedback | cicd | calendar
    expires_at: When | None = None
    reason: str | None = None
    by: str | None = None
    # exclude_window
    start: When | None = None
    end: When | None = None
    apply_to: list[Literal["train", "detect"]] | None = None
    # reset_point
    reset_at: When | None = None
    warmup: Dur | None = None
    at: When | None = None
    # recurring_override
    schedule: RecurringSchedule | None = None
    effect: RecurringEffect | None = None
    # override（detector/baseline/model.OVERRIDE_FIELDS）
    resolution: Dur | None = None
    z: NonNeg | None = None
    min_abs: NonNeg | None = None
    confirm_points: PosInt | None = None
    instant_z: NonNeg | None = None
    min_count: NonNeg | None = None


# ── calendar.yaml ──

class CalendarEntry(Strict):
    """一天（date）或一段（start＋end）；detector/adjust/adjust.calendar_adjustments。"""
    date: When | None = None
    start: When | None = None
    end: When | None = None
    name: str | None = None
    type: str | None = None          # holiday | maintenance | event（只是記錄用）
    match: dict[str, Any] | None = None
    series: str | None = None
    detect: bool | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.date is None and (self.start is None or self.end is None):
            raise ValueError("要有 date，或 start＋end")
        return self


class CalendarConfig(Strict):
    timezone: str | None = None
    sources: list[Provider] | None = None
    calendar: list[CalendarEntry] | None = None


# ── adjustments.yaml ──

class AdjustmentsConfig(Strict):
    adjustments: list[Adjustment] | None = None

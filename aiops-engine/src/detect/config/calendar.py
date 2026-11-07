"""calendar.yaml：行事曆（假日、維護日、活動）。"""
from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import model_validator

from common.config.common import Strict, Tz, When
from detect.config.types import Provider


# ── calendar.yaml ──

class CalendarEntry(Strict):
    """一天（date）或一段（start＋end）；detector/adjust.calendar_adjustments。"""
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
    timezone: Tz | None = None
    sources: list[Provider] = []             # 外部來源（file／http／taiwan）；沒寫也行
    calendar: list[CalendarEntry] = []       # 直接寫的項目（人工補的維護日、活動）

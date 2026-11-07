"""adjustments.yaml：調整（排除時段、重置點、週期放寬、門檻覆寫）。"""
from __future__ import annotations

from typing import Any, Literal

from shared.config.common import Dur, NonNeg, PosInt, Strict, When


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
    """一筆調整（detector/adjust.py）：series.yaml 的 overrides、adjustments.yaml 的 adjustments 同一種。kind 各自用到的欄位不同，
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
    # override（detector/model.OVERRIDE_FIELDS）
    resolution: Dur | None = None
    z: NonNeg | None = None
    min_abs: NonNeg | None = None
    confirm_points: PosInt | None = None
    instant_z: NonNeg | None = None
    min_count: NonNeg | None = None


# ── adjustments.yaml ──

class AdjustmentsConfig(Strict):
    adjustments: list[Adjustment] | None = None

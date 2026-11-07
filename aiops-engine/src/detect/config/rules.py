"""rules.yaml：靜態臨界值規則。"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from common.config.common import Dur, NonNeg, Num, Op, PosInt, Query, Ratio, Severity, Show, Strict
from detect.config.types import Value


# ── rules.yaml ──

class Join(Strict):
    field: str


class Rule(Strict):
    """一條規則（detector/rules.py 檔頭的說明）。type window 用 value；其他用 field。"""
    name: str
    type: Literal["value", "compare", "ratio", "increase", "delta", "increase_ratio", "rate", "total", "window", "slope"]
    index: str
    by: list[str]
    threshold: Num
    title: str | None = None
    description: str | None = None
    severity: Severity | None = None
    field: str | None = None
    other: str | None = None
    den: list[str] | None = None
    value: Value | None = None
    filter: Query | None = None
    exists: str | None = None
    join: Join | None = None
    hist_count: bool | None = None
    context: list[str] | None = None
    op: Op | None = None
    for_: Dur | int | None = Field(default=None, alias="for")
    fresh: Dur | None = None
    lookback: Dur | None = None
    abs: bool | None = None
    report_gone: bool | None = None
    min_ratio: Ratio | None = None
    min_events: NonNeg | None = None
    min_points: PosInt | None = None
    step_s: NonNeg | None = None
    scale: NonNeg | None = None
    show: Show | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.type == "window":
            if self.value is None:
                raise ValueError("type window 要有 value（寫法跟 series.yaml 一樣）")
        elif self.field is None:
            raise ValueError(f"type {self.type} 要有 field")
        if self.type == "compare" and self.other is None:
            raise ValueError("type compare 要有 other（要跟 field 相減的欄位）")
        if self.type in ("ratio", "increase_ratio") and not self.den:
            raise ValueError(f"type {self.type} 要有 den（分母欄位）")
        return self


class RulesConfig(Strict):
    interval: Dur | None = None              # 沒寫用 1 分鐘（Detector.interval_default）
    lookback: Dur = "10m"                    # 每條規則看最近多久（規則自己可以寫）
    rules: list[Rule] = []

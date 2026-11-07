"""quality.yaml：資料品質檢查。"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from shared.config.common import _same_length, Dur, NonNeg, Query, Ratio, Severity, Strict


# ── quality.yaml ──

class QualityCheck(Strict):
    """一個資料品質檢查（detector/quality.py 檔頭）。type 不同用的欄位不同，這裡全列。"""
    name: str
    type: Literal["cardinality", "missing", "stopped", "share", "lag", "present"]
    index: str
    title: str | None = None
    severity: Severity | None = None
    filter: Query | None = None
    by: list[str] | None = None
    keys: list[str] | None = None
    time_field: str | None = None
    window: Dur | None = None
    min_docs: NonNeg | None = None
    # cardinality
    field: str | None = None
    max_values: NonNeg | None = None
    ratio: NonNeg | None = None
    min_increase: NonNeg | None = None
    id_share: Ratio | None = None
    sample_values: NonNeg | None = None
    # missing
    fields: list[str] | None = None
    missing_if_equals: dict[str, str] | None = None
    max_missing: Ratio | None = None
    max_increase: Ratio | None = None
    # stopped
    lookback: Dur | None = None
    silent: Dur | None = None
    # share
    part_index: str | None = None
    max_share: Ratio | None = None
    # lag
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    stat: Literal["p50", "p95"] | None = None
    max_seconds: NonNeg | None = None
    absolute: bool | None = None

    @model_validator(mode="after")
    def _check(self):
        _same_length(self.keys, self.by)
        need = {"cardinality": ("field",), "missing": ("fields",), "share": ("part_index",), "lag": ("from_", "to")}
        for f in need.get(self.type, ()):
            if getattr(self, f) is None:
                raise ValueError(f"type {self.type} 要有 {f.rstrip('_')}")
        return self


class QualityConfig(Strict):
    interval: Dur | None = None
    window: Dur | None = None
    ref_offset: Dur | None = None
    checks: list[QualityCheck] | None = None

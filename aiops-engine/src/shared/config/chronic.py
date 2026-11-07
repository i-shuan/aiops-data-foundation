"""chronic.yaml：慢性病清單。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from shared.config.common import _same_length, NonNeg, PosInt, Provider, Query, Strict


# ── chronic.yaml ──

class ChronicSource(Strict):
    name: str
    type: Literal["log", "anomalies"]
    title: str | None = None
    index: str | None = None
    filter: Query | None = None
    by: list[str] | None = None
    key_names: list[str] | None = Field(default=None, alias="keys")   # yaml 的鍵叫 keys；屬性不能叫 keys（會蓋掉過渡層 dict 介面的 keys()）
    min_per_day: NonNeg | None = None
    min_days: PosInt | None = None
    top: PosInt | None = None
    levels: list[str] | None = None
    series: str | None = None

    @model_validator(mode="after")
    def _check(self):
        _same_length(self.key_names, self.by)
        if self.type == "log" and (self.index is None or not self.by):
            raise ValueError("type log 要有 index、by")
        return self


class ChronicConfig(Strict):
    days: PosInt | None = None
    min_days: PosInt | None = None
    top: PosInt | None = None
    timezone: str | None = None
    sources: list[ChronicSource] | None = None
    owners: list[dict[str, Any]] | None = None
    owners_source: list[Provider] | Provider | None = None

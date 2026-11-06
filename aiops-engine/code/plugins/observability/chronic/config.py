"""chronic.yaml 的設定模型（慢性病清單；引擎在 chronic.py、command.py）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import model_validator

from core.config import NonNeg, PosInt, Query, Strict, same_length
from sources.config import Provider


class ChronicSource(Strict):
    name: str
    type: Literal["log", "anomalies"]
    title: str | None = None
    index: str | None = None
    filter: Query | None = None
    by: list[str] | None = None
    keys: list[str] | None = None
    min_per_day: NonNeg | None = None
    min_days: PosInt | None = None
    top: PosInt | None = None
    levels: list[str] | None = None
    series: str | None = None

    @model_validator(mode="after")
    def _check(self):
        same_length(self.keys, self.by)
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

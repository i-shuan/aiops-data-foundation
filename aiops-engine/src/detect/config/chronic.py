"""chronic.yaml：慢性病清單。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from common.config.common import _same_length, NonNeg, PosInt, Query, Strict, Tz
from detect.config.types import Provider


# ── chronic.yaml ──

class ChronicSource(Strict):
    name: str
    type: Literal["log", "anomalies"]
    title: str | None = None                 # 沒寫就用 name
    index: str | None = None
    filter: Query | None = None
    by: list[str] | None = None
    key_names: list[str] | None = Field(default=None, alias="keys")   # yaml 的鍵叫 keys；屬性叫 key_names（不跟 dict 的 keys() 混）
    min_per_day: NonNeg | None = None
    min_days: PosInt | None = None
    top: PosInt | None = None
    levels: list[str] = ["rule", "peer"]    # type anomalies 才用：哪些 level 的異常算
    series: str | None = None

    @model_validator(mode="after")
    def _check(self):
        _same_length(self.key_names, self.by)
        if self.type == "log" and (self.index is None or not self.by):
            raise ValueError("type log 要有 index、by")
        return self


class ChronicConfig(Strict):
    days: PosInt = 7                         # 看最近幾個「完整的日子」
    min_days: PosInt = 6                     # 幾天裡至少幾天有才算慢性（來源可以自己寫）
    top: PosInt = 10
    timezone: Tz = "Asia/Taipei"
    sources: list[ChronicSource] = []
    owners: list[dict[str, Any]] = []
    owners_source: list[Provider] | Provider | None = None

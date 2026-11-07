"""ingest.yaml：接別人判好的異常。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from shared.config.common import Query, Strict


# ── ingest.yaml ──

class Pick(Strict):
    """{field, map, default}：讀該列的欄位、查表對應（detector/ingest._pick）。"""
    field: str
    map: dict[Any, Any] | None = None
    default: str | None = None


class Input(Strict):
    """一個外部來源（detector/ingest.py）；validate() 另外擋 name／index／series／keys 缺的。"""
    name: str
    index: str
    series: str
    key_names: dict[str, str] = Field(alias="keys")      # yaml 的鍵叫 keys（見 series.py 的說明）
    time_field: str | None = None
    filter: Query | None = None
    id_field: str | None = None
    value: str | None = None
    expected: str | None = None
    score: str | None = None
    level: Literal["ml", "fdc", "external"] | None = None
    status: str | Pick | None = None
    severity: str | Pick | None = None
    direction: str | Pick | None = None
    message: str | None = None
    extra: list[str] | None = None


class IngestConfig(Strict):
    timezone: str | None = None
    inputs: list[Input] | None = None

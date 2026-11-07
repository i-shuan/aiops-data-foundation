"""paths.yaml：工廠履歷合成（MES 進出站＋機台事件）。"""
from __future__ import annotations

from common.config.common import Query, Strict


# ── paths.yaml（工廠 plugin）──

class MesFields(Strict):
    lot: str
    equipment: str
    start: str
    step: str | None = None
    station: str | None = None
    chamber: str | None = None
    recipe: str | None = None
    recipe_version: str | None = None
    product: str | None = None
    end: str | None = None
    result: str | None = None


class Mes(Strict):
    index: str
    fields: MesFields
    time_field: str | None = None
    filter: Query | None = None
    bad_results: list[str] | None = None
    attrs: list[str] | None = None


class EventFields(Strict):
    lot: str
    start: str
    equipment: str | None = None
    chamber: str | None = None
    step: str | None = None
    end: str | None = None
    event: str | None = None


class Events(Strict):
    index: str
    fields: EventFields
    time_field: str | None = None
    filter: Query | None = None
    attrs: list[str] | None = None


class PathsConfig(Strict):
    timezone: str = "Asia/Taipei"
    group: str | None = None                 # 履歷索引依這個分（沒寫是 unassigned）
    group_field: str | None = None
    dept_field: str | None = None
    mes: Mes                                 # MES 進出站（一定要有）
    events: Events | None = None             # 機台事件（S6F11），沒有就只有站

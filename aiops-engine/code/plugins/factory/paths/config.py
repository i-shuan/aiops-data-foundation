"""paths.yaml 的設定模型（工廠的履歷合成 paths-merge）。"""
from __future__ import annotations

from core.config import Query, Strict
from sources.config import Source, Store


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
    timezone: str | None = None
    group: str | None = None
    group_field: str | None = None
    dept_field: str | None = None
    source: Source | None = None
    store: Store | None = None
    mes: Mes | None = None
    events: Events | None = None

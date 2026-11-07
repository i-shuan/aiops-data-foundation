"""對應 chart/registries/ChartComponentRegistrySingleton.ts（Superset 登記元件，我們登記真正做事的 run）。key＝`<段>.<名字>`。"""
from __future__ import annotations

from collections.abc import Callable

from core.models.registry import Registry

_registry: Registry[Callable] = Registry("PipelineRunRegistry")


def get_pipeline_run_registry() -> Registry[Callable]:
    return _registry

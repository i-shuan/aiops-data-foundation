"""對應 chart/registries/ChartControlPanelRegistrySingleton.ts。key＝`<段>.<名字>`。"""
from __future__ import annotations

from core.controls.types import ControlPanelConfig
from core.models.registry import Registry

_registry: Registry[ControlPanelConfig] = Registry("PipelineControlPanelRegistry")


def get_pipeline_control_panel_registry() -> Registry[ControlPanelConfig]:
    return _registry

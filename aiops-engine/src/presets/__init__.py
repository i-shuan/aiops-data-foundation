"""Preset（對應 superset-frontend/src/visualizations/presets/）。"""
from presets.factory import FactoryPreset
from presets.main_preset import MainPreset
from presets.metrics_only import MetricsOnlyPreset
from presets.observability import ObservabilityPreset

PRESETS = {"observability": ObservabilityPreset, "factory": FactoryPreset, "metrics_only": MetricsOnlyPreset}


def setup_plugins(scenario_name: str | None = None) -> None:
    """入口啟動時呼叫（對應 Superset 的 setupPlugins()）：照情境登記它的 Preset；不認得的情境（自己寫的 yaml、套件）用 MainPreset。"""
    PRESETS.get(scenario_name or "", MainPreset)().register()


__all__ = ["FactoryPreset", "MainPreset", "MetricsOnlyPreset", "ObservabilityPreset", "PRESETS", "setup_plugins"]

"""框架（對應 @superset-ui/core＋@superset-ui/chart-controls）：不認得任何業務、不 import 其他層。
結構與命名對照：docs/pdca/meta/2026-11-superset-style.md。"""
from core.controls import ControlConfig, ControlPanelConfig, ControlPanelSectionConfig, shared_controls
from core.models import OverwritePolicy, Plugin, Preset, Registry
from core.pipeline import (PipelineMetadata, PipelinePlugin, PipelineProps, get_pipeline_control_panel_registry,
                           get_pipeline_metadata_registry, get_pipeline_run_registry, pipeline_key, registered)

__all__ = ["ControlConfig", "ControlPanelConfig", "ControlPanelSectionConfig", "OverwritePolicy", "PipelineMetadata", "PipelinePlugin",
           "PipelineProps", "Plugin", "Preset", "Registry", "get_pipeline_control_panel_registry", "get_pipeline_metadata_registry",
           "get_pipeline_run_registry", "pipeline_key", "registered", "shared_controls"]

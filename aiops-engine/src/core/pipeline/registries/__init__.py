"""對應 @superset-ui/core 的 chart/registries/。"""
from core.pipeline.registries.control_panel import get_pipeline_control_panel_registry
from core.pipeline.registries.metadata import get_pipeline_metadata_registry
from core.pipeline.registries.run import get_pipeline_run_registry

__all__ = ["get_pipeline_control_panel_registry", "get_pipeline_metadata_registry", "get_pipeline_run_registry"]

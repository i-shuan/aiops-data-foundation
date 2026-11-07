"""PipelinePlugin（對應 @superset-ui/core 的 chart/models/ChartPlugin.ts）：一個外掛。

    class GoldenSignalsPipelinePlugin(PipelinePlugin):
        def __init__(self):
            super().__init__(stage="severity", metadata=PipelineMetadata(...), control_panel=control_panel,
                             load_run=lambda: golden_signals)

    GoldenSignalsPipelinePlugin().configure({"key": "golden_signals"}).register()

register 把三樣東西放進三張登記表（ChartPlugin 登記 metadata／controlPanel／元件，我們登記 metadata／control_panel／run），
key＝`<段>.<名字>`。run 的長相照那一段的契約（common/stages.py 的 contract）；load_run 是延遲載入（第一次用才 import）。
transform_props（選填）：PipelineProps → run 要的參數，跟 Superset 一樣放在外掛裡。
"""
from __future__ import annotations

from collections.abc import Callable

from core.controls.types import ControlPanelConfig
from core.models.plugin import Plugin
from core.pipeline.models.metadata import PipelineMetadata
from core.pipeline.models.props import PipelineProps
from core.pipeline.registries.control_panel import get_pipeline_control_panel_registry
from core.pipeline.registries.metadata import get_pipeline_metadata_registry
from core.pipeline.registries.run import get_pipeline_run_registry


def pipeline_key(stage: str, name: str) -> str:
    return f"{stage}.{name}"


class PipelinePlugin(Plugin):
    def __init__(self, stage: str, metadata: PipelineMetadata, control_panel: ControlPanelConfig | None = None,
                 run: Callable | None = None, load_run: Callable[[], Callable] | None = None,
                 transform_props: Callable[[PipelineProps], object] | None = None):
        super().__init__()
        if (run is None) == (load_run is None):
            raise ValueError("PipelinePlugin 要給 run 或 load_run 其中一個")
        self.stage = stage
        self.metadata = metadata
        self.control_panel = control_panel or ControlPanelConfig()
        self.run, self.load_run, self.transform_props = run, load_run, transform_props

    @property
    def key(self) -> str:
        name = self.config.get("key")
        if not name:
            raise ValueError(f"{type(self).__name__} 沒有 key：先 .configure({{'key': '名字'}})")
        return pipeline_key(self.stage, str(name))

    def register(self) -> "PipelinePlugin":
        key = self.key
        get_pipeline_metadata_registry().register_value(key, self.metadata)
        get_pipeline_control_panel_registry().register_value(key, self.control_panel)
        if self.load_run is not None:
            get_pipeline_run_registry().register_loader(key, self.load_run)
        else:
            get_pipeline_run_registry().register_value(key, self.run)
        return self

    def unregister(self) -> "PipelinePlugin":
        key = self.key
        for registry in (get_pipeline_metadata_registry(), get_pipeline_control_panel_registry(), get_pipeline_run_registry()):
            registry.remove(key)
        return self


def registered(stage: str) -> list[str]:
    """這一段登記了哪些外掛（登記順序）。"""
    prefix = f"{stage}."
    return [k[len(prefix):] for k in get_pipeline_metadata_registry().keys() if k.startswith(prefix)]

"""P0PipelinePlugin（⑥ 收斂：嚴重度）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_severity.p0.control_panel import control_panel


class P0PipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="severity",
            metadata=PipelineMetadata(name="p0", description="P0 服務（alerts.p0_services）升一級", tags=('severity',), category="severity",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_severity.p0.p0").run,   # 第一次用才 import
        )

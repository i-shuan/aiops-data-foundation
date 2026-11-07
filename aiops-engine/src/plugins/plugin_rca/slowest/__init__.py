"""SlowestPipelinePlugin（⑦ 根因分析：報告段落）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_rca.slowest.control_panel import control_panel


class SlowestPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="rca_sections",
            metadata=PipelineMetadata(name="slowest", description="最慢的請求的瀑布圖", tags=('rca',), category="rca_sections",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_rca.slowest.slowest").run,   # 第一次用才 import
        )

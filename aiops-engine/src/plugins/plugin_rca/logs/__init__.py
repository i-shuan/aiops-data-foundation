"""LogsPipelinePlugin（⑦ 根因分析：報告段落）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_rca.logs.control_panel import control_panel


class LogsPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="rca_sections",
            metadata=PipelineMetadata(name="logs", description="新的 log 樣式", tags=('rca',), category="rca_sections",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_rca.logs.logs").run,   # 第一次用才 import
        )

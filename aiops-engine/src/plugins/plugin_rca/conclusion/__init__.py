"""ConclusionPipelinePlugin（⑦ 根因分析：報告段落）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_rca.conclusion.control_panel import control_panel


class ConclusionPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="rca_sections",
            metadata=PipelineMetadata(name="conclusion", description="結論", tags=('rca',), category="rca_sections",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_rca.conclusion.conclusion").run,   # 第一次用才 import
        )

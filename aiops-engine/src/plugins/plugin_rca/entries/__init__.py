"""EntriesPipelinePlugin（⑦ 根因分析：報告段落）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_rca.entries.control_panel import control_panel


class EntriesPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="rca_sections",
            metadata=PipelineMetadata(name="entries", description="這個服務收到的請求跟平常比", tags=('rca',), category="rca_sections",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_rca.entries.entries").run,   # 第一次用才 import
        )

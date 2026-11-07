"""FieldsPipelinePlugin（⑦ 根因分析：相似事故）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_similar.fields.control_panel import control_panel


class FieldsPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="similar",
            metadata=PipelineMetadata(name="fields", description="欄位比對：同一段變慢、同一個錯誤源頭、同服務…（看得出為什麼像）", tags=(), category="similar",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_similar.fields.fields").run,   # 第一次用才 import
        )

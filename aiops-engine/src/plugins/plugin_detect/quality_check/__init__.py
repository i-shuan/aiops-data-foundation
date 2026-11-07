"""QualityCheckPipelinePlugin（④ 判斷）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_detect.quality_check.control_panel import control_panel


class QualityCheckPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="detect",
            metadata=PipelineMetadata(name="quality_check", description="資料品質檢查：路由沒參數化、欄位缺、資料停了", tags=(), category="detect",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_detect.quality_check.quality_check").run,   # 第一次用才 import
        )

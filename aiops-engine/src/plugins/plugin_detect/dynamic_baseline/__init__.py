"""DynamicBaselinePipelinePlugin（④ 判斷）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_detect.dynamic_baseline.control_panel import control_panel


class DynamicBaselinePipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="detect",
            metadata=PipelineMetadata(name="dynamic_baseline", description="動態基準線學習：每條序列 × 星期幾 × 小時的平常值，偏太多就報", tags=(), category="detect",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_detect.dynamic_baseline.dynamic_baseline").run,   # 第一次用才 import
        )

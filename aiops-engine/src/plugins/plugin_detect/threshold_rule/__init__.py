"""ThresholdRulePipelinePlugin（④ 判斷）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_detect.threshold_rule.control_panel import control_panel


class ThresholdRulePipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="detect",
            metadata=PipelineMetadata(name="threshold_rule", description="靜態臨界值規則：一定不能超過的線", tags=(), category="detect",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_detect.threshold_rule.threshold_rule").run,   # 第一次用才 import
        )

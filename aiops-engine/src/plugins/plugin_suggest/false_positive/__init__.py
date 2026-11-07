"""FalsePositivePipelinePlugin（⑨ 回饋：調整建議）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_suggest.false_positive.control_panel import control_panel


class FalsePositivePipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="suggest",
            metadata=PipelineMetadata(name="false_positive", description="誤報：放寬門檻、時段放寬、至少差多少才報", tags=(), category="suggest",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_suggest.false_positive.false_positive").run,   # 第一次用才 import
        )

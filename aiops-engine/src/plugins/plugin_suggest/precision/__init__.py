"""PrecisionPipelinePlugin（⑨ 回饋：調整建議）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_suggest.precision.control_panel import control_panel


class PrecisionPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="suggest",
            metadata=PipelineMetadata(name="precision", description="整條序列的準確率：太低放寬、太高收緊", tags=(), category="suggest",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_suggest.precision.precision").run,   # 第一次用才 import
        )

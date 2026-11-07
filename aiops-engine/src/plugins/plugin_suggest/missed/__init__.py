"""MissedPipelinePlugin（⑨ 回饋：調整建議）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_suggest.missed.control_panel import control_panel


class MissedPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="suggest",
            metadata=PipelineMetadata(name="missed", description="漏報：門檻降成 0.8 倍", tags=(), category="suggest",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_suggest.missed.missed").run,   # 第一次用才 import
        )

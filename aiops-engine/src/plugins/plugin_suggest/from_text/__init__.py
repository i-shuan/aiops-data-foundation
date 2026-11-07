"""FromTextPipelinePlugin（⑨ 回饋：調整建議）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_suggest.from_text.control_panel import control_panel


class FromTextPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="suggest",
            metadata=PipelineMetadata(name="from_text", description="回饋的一句話：LLM 抽成調整（送出回饋時）", tags=(), category="suggest",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_suggest.from_text.from_text").run,   # 第一次用才 import
        )

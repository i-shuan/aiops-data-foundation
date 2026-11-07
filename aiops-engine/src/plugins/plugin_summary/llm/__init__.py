"""LlmPipelinePlugin（⑦ 根因分析：摘要）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_summary.llm.control_panel import control_panel


class LlmPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="summary",
            metadata=PipelineMetadata(name="llm", description="LLM 摘要（只加註、每句附依據；provider 由 LLM_PROVIDER 決定）", tags=(), category="summary",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_summary.llm.llm").run,   # 第一次用才 import
        )

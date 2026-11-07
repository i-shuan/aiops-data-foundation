"""ChronicIssuePipelinePlugin（④ 判斷）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_detect.chronic_issue.control_panel import control_panel


class ChronicIssuePipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="detect",
            metadata=PipelineMetadata(name="chronic_issue", description="慢性病清單：天天噴、沒人修的（每週）", tags=(), category="detect",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_detect.chronic_issue.chronic_issue").run,   # 第一次用才 import
        )

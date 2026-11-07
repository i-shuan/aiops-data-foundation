"""TraceLatencyPipelinePlugin（④ 判斷）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_detect.trace_latency.control_panel import control_panel


class TraceLatencyPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="detect",
            metadata=PipelineMetadata(name="trace_latency", description="trace 每一段的統計與偵測", tags=(), category="detect",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_detect.trace_latency.trace_latency").run,   # 第一次用才 import
        )

"""GoldenSignalsPipelinePlugin（⑥ 收斂：嚴重度）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_severity.golden_signals.control_panel import control_panel


class GoldenSignalsPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="severity",
            metadata=PipelineMetadata(name="golden_signals", description="幾類黃金訊號、持續多久、換版後上限 warning", tags=('severity',), category="severity",
                                      behaviors=('needs_deploy_time',)),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_severity.golden_signals.golden_signals").run,   # 第一次用才 import
        )

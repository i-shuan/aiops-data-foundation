"""SameEntityPipelinePlugin（⑥ 收斂：併件）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_grouping.same_entity.control_panel import control_panel


class SameEntityPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="grouping",
            metadata=PipelineMetadata(name="same_entity", description="同一個對象（服務）、時間接著來的合成一件；跨服務、前後站照情境的 chain", tags=(), category="grouping",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_grouping.same_entity.same_entity").run,   # 第一次用才 import
        )

"""PeerComparePipelinePlugin（④ 判斷）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_detect.peer_compare.control_panel import control_panel


class PeerComparePipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="detect",
            metadata=PipelineMetadata(name="peer_compare", description="Pod 同儕橫向比對：同服務的 Pod 只有一隻不對勁", tags=(), category="detect",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_detect.peer_compare.peer_compare").run,   # 第一次用才 import
        )

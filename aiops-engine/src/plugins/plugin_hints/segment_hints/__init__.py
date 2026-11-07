"""SegmentHintsPipelinePlugin（⑦ 根因分析：歷史 case）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_hints.segment_hints.control_panel import control_panel


class SegmentHintsPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="hints",
            metadata=PipelineMetadata(name="segment_hints", description="線索：這次已經看到變化、過去也出事過的段，件數多的排前面", tags=(), category="hints",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_hints.segment_hints.segment_hints").run,   # 第一次用才 import
        )

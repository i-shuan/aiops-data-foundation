"""ChecklistPipelinePlugin（⑦ 根因分析：歷史 case）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_hints.checklist.control_panel import control_panel


class ChecklistPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="hints",
            metadata=PipelineMetadata(name="checklist", description="先查什麼：同一類事故老師傅勾過的排前面，不夠用情境的 checklists 補", tags=(), category="hints",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_hints.checklist.checklist").run,   # 第一次用才 import
        )

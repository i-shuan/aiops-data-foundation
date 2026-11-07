"""VectorPipelinePlugin（⑦ 根因分析：相似事故）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_similar.vector.control_panel import control_panel


class VectorPipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="similar",
            metadata=PipelineMetadata(name="vector", description="向量輔助：說法不同但意思一樣（要設 EMBEDDING_URL、EMBEDDING_MODEL）", tags=(), category="similar",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_similar.vector.vector").run,   # 第一次用才 import
        )

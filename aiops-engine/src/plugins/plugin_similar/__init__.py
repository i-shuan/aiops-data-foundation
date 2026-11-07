"""plugin_similar（⑦ 根因分析：相似事故）。對應 plugin-chart-*/src/index.ts：匯出這一包的全部外掛。"""
from plugins.plugin_similar.fields import FieldsPipelinePlugin
from plugins.plugin_similar.vector import VectorPipelinePlugin

__all__ = ["FieldsPipelinePlugin", "VectorPipelinePlugin"]

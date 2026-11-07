"""plugin_grouping（⑥ 收斂：併件）。對應 plugin-chart-*/src/index.ts：匯出這一包的全部外掛。"""
from plugins.plugin_grouping.same_entity import SameEntityPipelinePlugin

__all__ = ["SameEntityPipelinePlugin"]

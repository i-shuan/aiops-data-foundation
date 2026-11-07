"""plugin_severity（⑥ 收斂：嚴重度）。對應 plugin-chart-*/src/index.ts：匯出這一包的全部外掛。"""
from plugins.plugin_severity.golden_signals import GoldenSignalsPipelinePlugin
from plugins.plugin_severity.p0 import P0PipelinePlugin

__all__ = ["GoldenSignalsPipelinePlugin", "P0PipelinePlugin"]

"""plugin_summary（⑦ 根因分析：摘要）。對應 plugin-chart-*/src/index.ts：匯出這一包的全部外掛。"""
from plugins.plugin_summary.llm import LlmPipelinePlugin

__all__ = ["LlmPipelinePlugin"]

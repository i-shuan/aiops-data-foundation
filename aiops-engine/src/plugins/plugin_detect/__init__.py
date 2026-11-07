"""plugin_detect（④ 判斷）。對應 plugin-chart-*/src/index.ts：匯出這一包的全部外掛。"""
from plugins.plugin_detect.dynamic_baseline import DynamicBaselinePipelinePlugin
from plugins.plugin_detect.peer_compare import PeerComparePipelinePlugin
from plugins.plugin_detect.threshold_rule import ThresholdRulePipelinePlugin
from plugins.plugin_detect.chronic_issue import ChronicIssuePipelinePlugin
from plugins.plugin_detect.quality_check import QualityCheckPipelinePlugin
from plugins.plugin_detect.level_shift import LevelShiftPipelinePlugin
from plugins.plugin_detect.trace_latency import TraceLatencyPipelinePlugin

__all__ = ["DynamicBaselinePipelinePlugin", "PeerComparePipelinePlugin", "ThresholdRulePipelinePlugin", "ChronicIssuePipelinePlugin", "QualityCheckPipelinePlugin", "LevelShiftPipelinePlugin", "TraceLatencyPipelinePlugin"]

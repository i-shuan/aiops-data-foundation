"""MainPreset（對應 superset-frontend/src/visualizations/presets/MainPreset.js）：全部內建外掛。
登記順序就是各段的預設順序（沒寫 blocks: 的段照這個順序跑）。入口（main.py）啟動時 MainPreset().register()；測試在 conftest。"""
from __future__ import annotations

from core import Preset

from plugins.plugin_detect import DynamicBaselinePipelinePlugin, PeerComparePipelinePlugin, ThresholdRulePipelinePlugin, ChronicIssuePipelinePlugin, QualityCheckPipelinePlugin, LevelShiftPipelinePlugin, TraceLatencyPipelinePlugin
from plugins.plugin_grouping import SameEntityPipelinePlugin
from plugins.plugin_severity import GoldenSignalsPipelinePlugin, P0PipelinePlugin
from plugins.plugin_rca import AnomaliesPipelinePlugin, EntriesPipelinePlugin, SegmentsPipelinePlugin, SlowestPipelinePlugin, LogsPipelinePlugin, ChangesPipelinePlugin, TopologyPipelinePlugin, ConclusionPipelinePlugin
from plugins.plugin_similar import FieldsPipelinePlugin, VectorPipelinePlugin
from plugins.plugin_summary import LlmPipelinePlugin
from plugins.plugin_hints import SegmentHintsPipelinePlugin, ChecklistPipelinePlugin
from plugins.plugin_suggest import FalsePositivePipelinePlugin, MissedPipelinePlugin, PrecisionPipelinePlugin, RuleUpgradePipelinePlugin, FromTextPipelinePlugin


class MainPreset(Preset):
    def __init__(self):
        super().__init__(
            name="main",
            description="全部內建外掛",
            plugins=[
                DynamicBaselinePipelinePlugin().configure({"key": "dynamic_baseline"}),
                PeerComparePipelinePlugin().configure({"key": "peer_compare"}),
                ThresholdRulePipelinePlugin().configure({"key": "threshold_rule"}),
                ChronicIssuePipelinePlugin().configure({"key": "chronic_issue"}),
                QualityCheckPipelinePlugin().configure({"key": "quality_check"}),
                LevelShiftPipelinePlugin().configure({"key": "level_shift"}),
                TraceLatencyPipelinePlugin().configure({"key": "trace_latency"}),
                SameEntityPipelinePlugin().configure({"key": "same_entity"}),
                GoldenSignalsPipelinePlugin().configure({"key": "golden_signals"}),
                P0PipelinePlugin().configure({"key": "p0"}),
                AnomaliesPipelinePlugin().configure({"key": "anomalies"}),
                EntriesPipelinePlugin().configure({"key": "entries"}),
                SegmentsPipelinePlugin().configure({"key": "segments"}),
                SlowestPipelinePlugin().configure({"key": "slowest"}),
                LogsPipelinePlugin().configure({"key": "logs"}),
                ChangesPipelinePlugin().configure({"key": "changes"}),
                TopologyPipelinePlugin().configure({"key": "topology"}),
                ConclusionPipelinePlugin().configure({"key": "conclusion"}),
                FieldsPipelinePlugin().configure({"key": "fields"}),
                VectorPipelinePlugin().configure({"key": "vector"}),
                LlmPipelinePlugin().configure({"key": "llm"}),
                SegmentHintsPipelinePlugin().configure({"key": "segment_hints"}),
                ChecklistPipelinePlugin().configure({"key": "checklist"}),
                FalsePositivePipelinePlugin().configure({"key": "false_positive"}),
                MissedPipelinePlugin().configure({"key": "missed"}),
                PrecisionPipelinePlugin().configure({"key": "precision"}),
                RuleUpgradePipelinePlugin().configure({"key": "rule_upgrade"}),
                FromTextPipelinePlugin().configure({"key": "from_text"}),
            ],
        )

"""RuleUpgradePipelinePlugin（⑨ 回饋：調整建議）。對應 plugin-chart-*/src/<圖表>/index.ts：metadata、control panel、怎麼載入 run。"""
from __future__ import annotations

import importlib

from core import PipelineMetadata, PipelinePlugin

from plugins.plugin_suggest.rule_upgrade.control_panel import control_panel


class RuleUpgradePipelinePlugin(PipelinePlugin):
    def __init__(self):
        super().__init__(
            stage="suggest",
            metadata=PipelineMetadata(name="rule_upgrade", description="同根因確認 N 次：升級成靜態臨界值規則", tags=(), category="suggest",
                                      behaviors=()),
            control_panel=control_panel,
            load_run=lambda: importlib.import_module("plugins.plugin_suggest.rule_upgrade.rule_upgrade").run,   # 第一次用才 import
        )

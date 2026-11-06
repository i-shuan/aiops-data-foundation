"""只有指標：ML／FDC 判好的異常接進 ⑤（detector ingest），**沒有** MES 履歷。跟 factory 差在：
不併跨機台（沒有履歷就不知道前後站）、報告沒有履歷那幾段（只有異常、log、事故前的變更、結論）、沒有 paths-merge。
機台的 log、PM／recipe 變更、分類表、參數家族都跟 factory 一樣。configs/metrics_only/。
"""
from __future__ import annotations

from aiops.scenario.base import Chain

from aiops.scenario.factory import Plugin as Factory


class Plugin(Factory):
    name = "metrics_only"
    sections = ("anomalies", "logs", "changes", "conclusion")
    schedule = "configs/metrics_only/schedule.yaml"

    def chain(self, es, cfg, anomalies, open_alerts, start, now) -> Chain:
        return Chain()

    def commands(self) -> dict:
        return {}

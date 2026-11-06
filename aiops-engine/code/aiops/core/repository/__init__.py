"""repository 層：索引名與 ES 的查詢只出現在這裡；service 透過 Repos 拿各實體的 repository。

    repo = Repos(ES())          # 指令開頭；測試給假 ES（只要有 req／search／bulk_index／ensure_index），或 tests/fakes.MemoryRepos 不連 ES
    repo.alerts.open()          # ⑥ 開著的件
    repo.anomalies.write(docs)  # ⑤ 寫異常（補 engine_version、歸屬）
    repo.series.fetch(spec, start, end, "5m")   # 原始資料 → 一條序列
"""
from __future__ import annotations

from aiops.core.repository._base import Repository
from aiops.core.repository.alert import AlertRepository
from aiops.core.repository.anomaly import AnomalyRepository
from aiops.core.repository.baseline import BaselineRepository
from aiops.core.repository.change import ChangeRepository
from aiops.core.repository.chronic import ChronicRepository
from aiops.core.repository.feedback import FeedbackRepository
from aiops.core.repository.housekeeping import HousekeepingRepository
from aiops.core.repository.incident import IncidentRepository
from aiops.core.repository.log import LogRepository
from aiops.core.repository.quality import QualityRepository
from aiops.core.repository.segment import SegmentRepository
from aiops.core.repository.series import SeriesRepository, fill_zero
from aiops.core.repository.state import StateRepository
from aiops.core.repository.trace import TraceRepository
from aiops.core.repository.tuning import TuningRepository

__all__ = ["Repos", "Repository", "AlertRepository", "AnomalyRepository", "BaselineRepository", "ChangeRepository", "ChronicRepository",
           "FeedbackRepository", "HousekeepingRepository", "IncidentRepository", "LogRepository", "QualityRepository", "SegmentRepository",
           "SeriesRepository", "StateRepository", "TraceRepository", "TuningRepository", "fill_zero"]


class Repos:
    """一條連線上全部實體的 repository。es 是 core.connection.ES（或測試的假 ES）。"""

    def __init__(self, es):
        self.es = es
        self.alerts = AlertRepository(es)
        self.anomalies = AnomalyRepository(es)
        self.baseline = BaselineRepository(es)
        self.change = ChangeRepository(es)
        self.chronic = ChronicRepository(es)
        self.feedback = FeedbackRepository(es)
        self.housekeeping = HousekeepingRepository(es)
        self.incidents = IncidentRepository(es)
        self.logs = LogRepository(es)
        self.quality = QualityRepository(es)
        self.segments = SegmentRepository(es)
        self.series = SeriesRepository(es)
        self.state = StateRepository(es)
        self.traces = TraceRepository(es)
        self.tuning = TuningRepository(es)

"""repository 層：索引名與 ES 的查詢只出現在這裡；service 透過 Repos 拿各實體的 repository。

    repo = Repos(ES(), load_scenario())   # 指令開頭（組合根）；測試給假 ES，或 tests/fakes.MemoryRepos 不連 ES
    repo.alerts.open()          # ⑥ 開著的件
    repo.anomalies.write(docs)  # ⑤ 寫異常（補 engine_version、歸屬）
    repo.series.fetch(spec, start, end, "5m")   # 原始資料 → 一條序列
"""
from __future__ import annotations

from core.repository._base import Repository
from core.repository.alert import AlertRepository
from core.repository.anomaly import AnomalyRepository
from core.repository.baseline import BaselineRepository
from core.repository.change import ChangeRepository
from core.repository.chronic import ChronicRepository
from core.repository.feedback import FeedbackRepository
from core.repository.housekeeping import HousekeepingRepository
from core.repository.incident import IncidentRepository
from core.repository.log import LogRepository
from core.repository.quality import QualityRepository
from core.repository.segment import SegmentRepository
from core.repository.series import SeriesRepository, fill_zero
from core.repository.state import StateRepository
from core.repository.trace import TraceRepository
from core.repository.tuning import TuningRepository

__all__ = ["Repos", "Repository", "AlertRepository", "AnomalyRepository", "BaselineRepository", "ChangeRepository", "ChronicRepository",
           "FeedbackRepository", "HousekeepingRepository", "IncidentRepository", "LogRepository", "QualityRepository", "SegmentRepository",
           "SeriesRepository", "StateRepository", "TraceRepository", "TuningRepository", "fill_zero"]


class Repos:
    """一條連線上全部實體的 repository，帶著這次的情境（scenario：原始資料的欄位對照、對象的 key；需要它的 repository 才會用到）。
    es 是 core.connection.ES（或測試的假 ES）；scenario 由組合根給（scenario.load_scenario），core 自己不載。"""

    def __init__(self, es, scenario=None):
        self.es, self.scenario = es, scenario
        self.alerts = AlertRepository(es, scenario)
        self.anomalies = AnomalyRepository(es, scenario)
        self.baseline = BaselineRepository(es, scenario)
        self.change = ChangeRepository(es, scenario)
        self.chronic = ChronicRepository(es, scenario)
        self.feedback = FeedbackRepository(es, scenario)
        self.housekeeping = HousekeepingRepository(es, scenario)
        self.incidents = IncidentRepository(es, scenario)
        self.logs = LogRepository(es, scenario)
        self.quality = QualityRepository(es, scenario)
        self.segments = SegmentRepository(es, scenario)
        self.series = SeriesRepository(es, scenario)
        self.state = StateRepository(es, scenario)
        self.traces = TraceRepository(es, scenario)
        self.tuning = TuningRepository(es, scenario)

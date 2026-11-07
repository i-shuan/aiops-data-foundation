"""兩條連線上全部實體的 repository：`Repos(es, scenario, db)`。

    repo = Repos(ES(), load_scenario())   # 指令開頭（組合根）；db 沒給就用環境變數 AIOPS_PG_DSN（第一次查詢才連）
                                          # 測試給假 ES／測試用的 PostgreSQL，或 tests/fakes.MemoryRepos 都不連
ES 是原始資料（Elastic APM 寫的）與 traces-merged-*；db 是 PostgreSQL，engine 自己的資料（common/infra/pg.py）。
    repo.alerts.open()          # ⑥ 開著的件
    repo.anomalies.write(docs)  # ⑤ 寫異常（補 engine_version、歸屬）
    repo.series.fetch(spec, start, end, "5m")   # 原始資料 → 一條序列

repository 分散在各功能資料夾的 store/（detect、incident、learn）。common 不能依賴功能資料夾，
所以這裡只放名稱表，**第一次取用才載入**那個 repository（importlib），載入後留著；所以 common 沒有靜態 import 任何功能。
scenario 由組合根給（common.scenario.load_scenario），這裡不載。"""
from __future__ import annotations

import importlib

from common.infra.pg import DB

# 屬性名 → "模組:類別"
REGISTRY = {
    "alerts": "incident.store.alert_repo:AlertRepository",
    "anomalies": "detect.store.anomaly_repo:AnomalyRepository",
    "baseline": "detect.store.baseline_repo:BaselineRepository",
    "change": "incident.store.change_repo:ChangeRepository",
    "chronic": "detect.store.chronic_repo:ChronicRepository",
    "feedback": "learn.store.feedback_repo:FeedbackRepository",
    "housekeeping": "detect.store.housekeeping_repo:HousekeepingRepository",
    "incidents": "incident.store.incident_repo:IncidentRepository",
    "incident_vectors": "incident.store.vector_repo:IncidentVectorRepository",
    "packed": "common.infra.pack:PackedRepository",
    "logs": "incident.store.log_repo:LogRepository",
    "quality": "detect.store.quality_repo:QualityRepository",
    "segments": "detect.store.segment_repo:SegmentRepository",
    "series": "data.store.series_repo:SeriesRepository",
    "state": "detect.store.state_repo:StateRepository",
    "traces": "data.store.trace_repo:TraceRepository",
    "tuning": "learn.store.tuning_repo:TuningRepository",
}


class Repos:
    def __init__(self, es, scenario=None, db: DB | None = None):
        self.es, self.scenario = es, scenario
        self.db = db if db is not None else DB()

    def __getattr__(self, name):
        target = REGISTRY.get(name)
        if target is None:
            raise AttributeError(name)
        module, cls = target.split(":")
        repo = getattr(importlib.import_module(module), cls)(self.es, self.scenario, db=self.db)
        setattr(self, name, repo)
        return repo

    def __dir__(self):
        return sorted({*super().__dir__(), *REGISTRY})

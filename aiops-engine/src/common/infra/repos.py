"""一條連線上全部實體的 repository：`Repos(es, scenario)`。

    repo = Repos(ES(), load_scenario())   # 指令開頭（組合根）；測試給假 ES，或 tests/fakes.MemoryRepos 不連 ES
    repo.alerts.open()          # ⑥ 開著的件
    repo.anomalies.write(docs)  # ⑤ 寫異常（補 engine_version、歸屬）
    repo.series.fetch(spec, start, end, "5m")   # 原始資料 → 一條序列

repository 分散在各功能資料夾的 store/（detect、incident、learn）。common 不能依賴功能資料夾，
所以這裡只放名稱表，**第一次取用才載入**那個 repository（importlib），載入後留著；所以 common 沒有靜態 import 任何功能。
scenario 由組合根給（common.scenario.load_scenario），這裡不載。"""
from __future__ import annotations

import importlib

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
    "logs": "incident.store.log_repo:LogRepository",
    "quality": "detect.store.quality_repo:QualityRepository",
    "segments": "detect.store.segment_repo:SegmentRepository",
    "series": "detect.store.series_repo:SeriesRepository",
    "state": "detect.store.state_repo:StateRepository",
    "traces": "detect.store.trace_repo:TraceRepository",
    "tuning": "learn.store.tuning_repo:TuningRepository",
}


class Repos:
    def __init__(self, es, scenario=None):
        self.es, self.scenario = es, scenario

    def __getattr__(self, name):
        target = REGISTRY.get(name)
        if target is None:
            raise AttributeError(name)
        module, cls = target.split(":")
        repo = getattr(importlib.import_module(module), cls)(self.es, self.scenario)
        setattr(self, name, repo)
        return repo

    def __dir__(self):
        return sorted({*super().__dir__(), *REGISTRY})

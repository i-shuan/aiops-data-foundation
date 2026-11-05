"""schedule.yaml 的設定模型（scheduler/runner.load_schedule 讀）。"""
from __future__ import annotations

from typing import Any, Literal

from core.config import NonNeg, PosInt, Strict


class Job(Strict):
    """scheduler/runner.Job.from_dict 讀的欄位。"""
    name: str
    cron: str
    args: list[Any]
    timeout: PosInt | None = None
    retries: NonNeg | None = None
    stage: PosInt | None = None
    why: str | None = None
    queue: Literal["realtime", "alert", "batch"] | None = None


class ScheduleConfig(Strict):
    timezone: str | None = None
    jobs: list[Job] | None = None

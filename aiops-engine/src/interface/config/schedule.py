"""schedule.yaml：排程表。"""
from __future__ import annotations

from typing import Any, Literal

from common.config.common import NonNeg, PosInt, Strict, Tz


# ── schedule.yaml ──

class Job(Strict):
    """排程表的一個工作（interface/jobs/runner.Job.from_config 讀）。"""
    name: str
    cron: str
    args: list[Any]
    timeout: PosInt = 300                    # 秒
    retries: NonNeg | None = None            # 沒寫：每分鐘的 0 次（下一輪就是重試）、其他 2 次，看 cron 才知道，所以留 None
    stage: PosInt = 1
    why: str = ""
    queue: Literal["realtime", "alert", "batch"] | None = None    # 沒寫：依名字與頻率挑（runner.default_queue）


class ScheduleConfig(Strict):
    timezone: Tz = "Asia/Taipei"
    jobs: list[Job] = []

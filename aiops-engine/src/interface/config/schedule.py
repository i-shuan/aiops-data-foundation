"""schedule.yaml：排程表。"""
from __future__ import annotations

from typing import Any, Literal

from common.config.common import NonNeg, PosInt, Strict, Tz
from common.config.loader import load_config


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


def trace_raw_pairs(schedule_path: str) -> list[tuple[str, str | None]] | None:
    """排程表裡 trace-merge 用的 trace.yaml 的 raw 索引（[(索引, 明講的來源)]）；/mapping 顯示 trace 的 yaml 值、提覆寫時算「原本是什麼」用。
    排程表沒有 trace-merge（工廠、只有 metrics）或讀不到：None。"""
    try:
        for job in load_config(schedule_path, "schedule").jobs:
            args = [str(a) for a in job.args]
            if "trace-merge" in args and "--config" in args[:-1]:
                return load_config(args[args.index("--config") + 1], "trace").raw_indexes()
    except (OSError, ValueError) as e:
        print(f"  ⚠ 讀不到排程表的 trace 設定（{schedule_path}：{str(e)[:100]}）")
    return None

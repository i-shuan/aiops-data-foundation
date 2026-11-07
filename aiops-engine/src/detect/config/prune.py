"""prune.yaml：engine 自己的資料（PostgreSQL）的保留天數。"""
from __future__ import annotations

from typing import Literal

from common.config.common import NonNeg, Strict, Tz


# ── prune.yaml ──

class PruneTable(Strict):
    # 哪一種資料（Repos 的名字）：anomalies 依異常的時間、segments 依統計的時間、alerts 只刪已結案的、依結案時間
    table: Literal["anomalies", "segments", "alerts"]
    keep_days: NonNeg


class PruneConfig(Strict):
    timezone: Tz = "Asia/Taipei"            # 輸出的時間用這個時區
    tables: list[PruneTable] = []

"""prune.yaml：AIOps 自己索引的保留天數。"""
from __future__ import annotations

from typing import Any

from shared.config.common import NonNeg, Strict


# ── prune.yaml ──

class PruneIndex(Strict):
    index: str
    keep_days: NonNeg
    time_field: str = "@timestamp"           # 依這個欄位的時間判斷「超過保留天數」
    filter: dict[str, Any] | None = None     # 再加上的條件，例如 aiops-alerts 只刪 status resolved


class PruneConfig(Strict):
    timezone: str = "Asia/Taipei"            # 輸出的時間用這個時區
    indices: list[PruneIndex] = []

"""prune.yaml：AIOps 自己索引的保留天數。"""
from __future__ import annotations

from typing import Any

from shared.config.common import NonNeg, Strict


# ── prune.yaml ──

class PruneIndex(Strict):
    index: str
    keep_days: NonNeg
    time_field: str | None = None
    filter: dict[str, Any] | None = None


class PruneConfig(Strict):
    timezone: str | None = None
    indices: list[PruneIndex] | None = None

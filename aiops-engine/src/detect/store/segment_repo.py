"""trace 每段的統計（PostgreSQL 的 aiops_trace_segments）：trace-detect 每 5 分鐘算一次（API × 段 × 時段），trace-train 從這裡學。"""
from __future__ import annotations

from datetime import datetime

from common.infra.pg import PgRepository

SEGMENT_TABLE = "aiops_trace_segments"


class SegmentRepository(PgRepository):
    TABLE, TIME = SEGMENT_TABLE, "@timestamp"
    INDEXES = ("((doc->>'kind'), ts)",)

    def segments(self, start: datetime, end: datetime) -> list[dict]:
        """[start, end) 的段統計（kind segment），依時間排。"""
        return [d for _, d in self._select("ts >= %s AND ts < %s AND doc->>'kind' = 'segment'", (start, end), order="ts, id")]

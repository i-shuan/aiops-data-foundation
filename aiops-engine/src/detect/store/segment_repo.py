"""trace 每段的統計（aiops-trace-segments）：trace-detect 每 5 分鐘算一次（API × 段 × 時段），trace-train 從這裡學。"""
from __future__ import annotations

from datetime import datetime

from shared.infra.es import time_range
from shared.infra.es import Repository

SEGMENT_INDEX = "aiops-trace-segments"
SEGMENT_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "kind": {"type": "keyword"},
    "entry": {"properties": {"service": {"type": "keyword"}, "name": {"type": "keyword"}}},
    "segment": {"type": "keyword"}, "requests": {"type": "integer"}, "calls": {"type": "double"}, "time_ms": {"type": "double"},
    "error_rate": {"type": "double"}, "swallowed_rate": {"type": "double"}, "exception_type": {"type": "keyword"},
    "caller": {"type": "keyword"}, "callee": {"type": "keyword"}, "skew_ms": {"type": "double"}}}}


class SegmentRepository(Repository):
    INDEX, MAPPING = SEGMENT_INDEX, SEGMENT_MAPPING

    def segments(self, start: datetime, end: datetime) -> list[dict]:
        """[start, end) 的段統計（kind segment），依時間排。"""
        body = {"query": {"bool": {"filter": [time_range(start, end), {"term": {"kind": "segment"}}]}}}
        return [h["_source"] for h in self._scan(body, size=10000)]

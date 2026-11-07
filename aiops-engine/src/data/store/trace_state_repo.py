"""trace-merge 的進度（ES 的 aiops-trace-state）：上次合到哪。跟 trace 放一起，PostgreSQL 不碰 trace。"""
from __future__ import annotations

from common.infra.es import Repository

TRACE_STATE_INDEX = "aiops-trace-state"
TRACE_STATE_MAPPING = {"mappings": {"dynamic": False, "properties": {"last": {"type": "date"}, "updated_at": {"type": "date"}}}}


class TraceStateRepository(Repository):
    INDEX, MAPPING = TRACE_STATE_INDEX, TRACE_STATE_MAPPING

    def get(self, key: str) -> dict | None:
        return self._get(key)

    def set(self, key: str, doc: dict) -> None:
        self.ensure()
        self._put(key, doc)

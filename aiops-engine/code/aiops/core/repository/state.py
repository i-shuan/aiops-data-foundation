"""排程工作的進度（aiops-state）：例如 trace-merge 上次合到哪。"""
from __future__ import annotations

from aiops.core.repository._base import Repository

STATE_INDEX = "aiops-state"
STATE_MAPPING = {"mappings": {"dynamic": False, "properties": {"last": {"type": "date"}, "updated_at": {"type": "date"}}}}


class StateRepository(Repository):
    INDEX, MAPPING = STATE_INDEX, STATE_MAPPING

    def get(self, key: str) -> dict | None:
        return self._get(key)

    def set(self, key: str, doc: dict) -> None:
        self.ensure()
        self._put(key, doc)

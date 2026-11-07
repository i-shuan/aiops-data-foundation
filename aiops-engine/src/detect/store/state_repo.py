"""排程工作的進度（PostgreSQL 的 aiops_state）：例如 trace-merge 上次合到哪。"""
from __future__ import annotations

from common.infra.pg import PgRepository

STATE_TABLE = "aiops_state"


class StateRepository(PgRepository):
    TABLE = STATE_TABLE

    def get(self, key: str) -> dict | None:
        return self._get(key)

    def set(self, key: str, doc: dict) -> None:
        self._put(key, doc)

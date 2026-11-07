"""慢性病清單（PostgreSQL 的 aiops_chronic）：每週一份，天天都有的告警、log（detect/strategy/chronic_issue.py）。"""
from __future__ import annotations

from common.infra.pg import PgRepository

CHRONIC_TABLE = "aiops_chronic"


class ChronicRepository(PgRepository):
    TABLE, TIME = CHRONIC_TABLE, "@timestamp"
    INDEXES = ("((doc->>'week'))",)

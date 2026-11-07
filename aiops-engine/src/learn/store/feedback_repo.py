"""⑨ 回饋（PostgreSQL 的 aiops_feedback）。"""
from __future__ import annotations

from datetime import datetime

from common.infra.pg import PgRepository, has
from learn.store.models import FEEDBACK_TABLE


def _rows(rows: list[tuple[str, dict]]) -> list[dict]:
    return [{**d, "_id": i} for i, d in rows]


class FeedbackRepository(PgRepository):
    TABLE, TIME = FEEDBACK_TABLE, "@timestamp"
    INDEXES = ("((doc->>'alert_id'))", "((doc->>'verdict'), ts)")

    def since(self, start: datetime, end: datetime | None = None) -> list[dict]:
        """@timestamp ≥ start（< end）的回饋，依時間排。"""
        if end is None:
            return _rows(self._select("ts >= %s", (start,), order="ts, id"))
        return _rows(self._select("ts >= %s AND ts < %s", (start, end), order="ts, id"))

    def missed_since(self, start: datetime, end: datetime | None = None, without_alert: bool = False) -> list[dict]:
        """漏報（verdict missed）。without_alert：只要沒有 alert_id 的（metrics：有 alert_id 的照 alert 算）。"""
        where, params = ["doc->>'verdict' = 'missed'", "ts >= %s"], [start]
        if end is not None:
            where.append("ts < %s")
            params.append(end)
        if without_alert:
            where.append(f"NOT {has('alert_id')}")
        return _rows(self._select(" AND ".join(where), params, order="ts, id"))

    def by_alert_ids(self, alert_ids: list[str]) -> list[dict]:
        """這些件的回饋（不管什麼時候給的）。"""
        ids = sorted({i for i in alert_ids if i})
        return _rows(self._select("doc->>'alert_id' = ANY(%s)", (ids,), order="ts, id")) if ids else []

    def put(self, _id: str, doc: dict) -> None:
        self._put(_id, doc)

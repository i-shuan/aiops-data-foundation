"""⑨ 回饋（aiops-feedback）。"""
from __future__ import annotations

from datetime import datetime

from learn.store.models import FEEDBACK_INDEX, FEEDBACK_MAPPING
from common.infra.es import Repository

TERMS_CHUNK = 1000   # 一次 terms 查幾個 alert_id：查詢字串別太長


def _rows(hits: list[dict]) -> list[dict]:
    return [{**h["_source"], "_id": h["_id"]} for h in hits]


class FeedbackRepository(Repository):
    INDEX, MAPPING = FEEDBACK_INDEX, FEEDBACK_MAPPING

    def since(self, start: datetime, end: datetime | None = None) -> list[dict]:
        """@timestamp ≥ start（< end）的回饋，依時間排。"""
        rng = {"gte": start.isoformat(), **({"lt": end.isoformat()} if end is not None else {})}
        return _rows(self._scan({"query": {"range": {"@timestamp": rng}}}))

    def missed_since(self, start: datetime, end: datetime | None = None, without_alert: bool = False) -> list[dict]:
        """漏報（verdict missed）。without_alert：只要沒有 alert_id 的（metrics：有 alert_id 的照 alert 算）。"""
        rng = {"gte": start.isoformat(), **({"lt": end.isoformat()} if end is not None else {})}
        q = {"bool": {"filter": [{"term": {"verdict": "missed"}}, {"range": {"@timestamp": rng}}]}}
        if without_alert:
            q["bool"]["must_not"] = [{"exists": {"field": "alert_id"}}]
        return _rows(self._scan({"query": q}))

    def by_alert_ids(self, alert_ids: list[str]) -> list[dict]:
        """這些件的回饋（不管什麼時候給的）。"""
        ids = sorted({i for i in alert_ids if i})
        out = []
        for i in range(0, len(ids), TERMS_CHUNK):
            out += _rows(self._scan({"query": {"bool": {"filter": [{"terms": {"alert_id": ids[i:i + TERMS_CHUNK]}}]}}}))
        return out

    def put(self, _id: str, doc: dict) -> None:
        self._put(_id, doc)

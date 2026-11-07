"""⑥ 告警（PostgreSQL 的 aiops_alerts）：一個服務一件；還有幾筆不是「件」的狀態文件（heartbeat、detect-status、source-check-*，status ok）。"""
from __future__ import annotations

from datetime import datetime

from common.infra.pg import PgRepository
from incident.store.models import ALERT_TABLE, Alert

LIST_FIELDS = ["alert_id", "service", "title", "summary", "severity", "status", "dept", "group", "started_at", "updated_at", "resolved_at",
               "anomaly_count", "series", "upstream_services", "suggested_category", "category_confidence", "report_url", "feedback_url"]


def _t(field: str) -> str:
    """文件裡的時間欄位轉成 timestamptz（排序、比較用；字串比不準：時區寫法不同）。"""
    return f"(doc->>'{field}')::timestamptz"


_PRUNE = f"doc->>'status' = 'resolved' AND {_t('resolved_at')} < %s"


class AlertRepository(PgRepository):
    TABLE, TIME = ALERT_TABLE, "@timestamp"
    INDEXES = ("((doc->>'status'))", "((doc->>'service'))")

    def get(self, alert_id: str) -> dict | None:
        return self._get(alert_id)

    def open(self) -> list[Alert]:
        """開著的件（Alert 模型）。"""
        return [Alert.from_doc(d) for _, d in self._select("doc->>'status' = 'open'", order="id")]

    def resolved_anomaly_ids(self, since: datetime) -> set[str]:
        """since 之後結束的件裡的異常 id：重跑時這些異常不再開新的一件。"""
        rows = self._select(f"doc->>'status' = 'resolved' AND {_t('resolved_at')} >= %s", (since,))
        return {i for _, d in rows for i in d.get("anomaly_ids") or []}

    def list(self, status: str = "open", size: int = 50) -> list[dict]:
        """件的清單（新的在前），不含報告全文。status：open／resolved／all。"""
        where, params = ("TRUE", ()) if status == "all" else ("doc->>'status' = %s", (status,))
        rows = self._select(where, params, order=f"{_t('updated_at')} DESC NULLS LAST, id", limit=size)
        return [{k: d[k] for k in LIST_FIELDS if k in d} for _, d in rows]

    def started_between(self, start: datetime, end: datetime) -> list[dict]:
        """started_at 在 [start, end) 的全部文件（含 _id；metrics 用）。"""
        rows = self._select(f"{_t('started_at')} >= %s AND {_t('started_at')} < %s", (start, end), order=f"{_t('started_at')}, id")
        return [{**d, "_id": i} for i, d in rows]

    def request_rca(self, alert_id: str, at: str) -> None:
        self._patch(alert_id, {"rca_requested": True, "rca_requested_at": at})

    def prune(self, cutoff: datetime) -> int:
        """只刪已結案、結案時間早於 cutoff 的；開著的件、狀態文件沒有 resolved_at，不會被刪。"""
        return self.delete_where(_PRUNE, (cutoff,))

    def prune_count(self, cutoff: datetime) -> int:
        return self.count(_PRUNE, (cutoff,))

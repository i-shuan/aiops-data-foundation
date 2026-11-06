"""⑥ 告警（aiops-alerts）：一個服務一件；還有兩筆不是「件」的狀態文件（heartbeat、detect-status，status ok）。"""
from __future__ import annotations

from datetime import datetime

from incident.store.alert import ALERT_INDEX, ALERT_MAPPING, Alert
from shared.repository import Repository

LIST_FIELDS = ["alert_id", "service", "title", "summary", "severity", "status", "dept", "group", "started_at", "updated_at", "resolved_at",
               "anomaly_count", "series", "upstream_services", "suggested_category", "category_confidence", "report_url", "feedback_url"]


class AlertRepository(Repository):
    INDEX, MAPPING = ALERT_INDEX, ALERT_MAPPING

    def get(self, alert_id: str) -> dict | None:
        return self._get(alert_id)

    def open(self) -> list[Alert]:
        """開著的件（Alert 模型）。"""
        return [Alert.from_doc(h["_source"]) for h in self._hits({"size": 10000, "query": {"term": {"status": "open"}}})]

    def resolved_anomaly_ids(self, since: datetime) -> set[str]:
        """since 之後結束的件裡的異常 id：重跑時這些異常不再開新的一件。"""
        body = {"size": 10000, "_source": ["anomaly_ids"], "query": {"bool": {"filter": [
            {"term": {"status": "resolved"}}, {"range": {"resolved_at": {"gte": since.isoformat()}}}]}}}
        return {i for h in self._hits(body) for i in h["_source"].get("anomaly_ids") or []}

    def list(self, status: str = "open", size: int = 50) -> list[dict]:
        """件的清單（新的在前），不含報告全文。status：open／resolved／all。"""
        q = {"match_all": {}} if status == "all" else {"term": {"status": status}}
        return [h["_source"] for h in self._hits({"size": size, "_source": LIST_FIELDS, "query": q, "sort": [{"updated_at": "desc"}]})]

    def started_between(self, start: datetime, end: datetime) -> list[dict]:
        """started_at 在 [start, end) 的全部文件（含 _id；metrics 用）。"""
        body = {"query": {"bool": {"filter": [{"range": {"started_at": {"gte": start.isoformat(), "lt": end.isoformat()}}}]}}}
        return [{**h["_source"], "_id": h["_id"]} for h in self._scan(body)]

    def request_rca(self, alert_id: str, at: str) -> None:
        self._patch(alert_id, {"rca_requested": True, "rca_requested_at": at})

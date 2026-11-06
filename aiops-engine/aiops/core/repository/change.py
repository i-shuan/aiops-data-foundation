"""「換版」的查詢：IT 看服務的版號（service.version）第一次出現；工廠看變更紀錄（recipe、設定）最近一筆。索引、欄位由場景給。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiops.core.repository._base import Repository


class ChangeRepository(Repository):
    def version_first_seen(self, index: str, service: str, since: datetime, now: datetime,
                           field: str = "service.version", service_field: str = "service.name") -> list[datetime]:
        """這個服務每個版本第一次出現的時間（since 往前 24 小時到 now），由早到晚。"""
        body = {"size": 0, "query": {"bool": {"filter": [{"term": {service_field: service}},
                                                           {"range": {"@timestamp": {"gte": (since - timedelta(hours=24)).isoformat(), "lt": now.isoformat()}}}]}},
                "aggs": {"v": {"terms": {"field": field, "size": 20}, "aggs": {"first": {"min": {"field": "@timestamp"}}}}}}
        buckets = self._search(body, index).get("aggregations", {}).get("v", {}).get("buckets", [])
        return sorted(datetime.fromtimestamp(b["first"]["value"] / 1000, tz=timezone.utc) for b in buckets if b.get("first", {}).get("value") is not None)

    def latest(self, index: str, entity_field: str, entity: str, since: datetime, now: datetime, time_field: str = "@timestamp",
               extra_filter: dict | None = None) -> datetime | None:
        """這個對象 since 之後最近一筆變更的時間；沒有回 None。index 是變更紀錄（一筆一次變更）。"""
        f = [{"term": {entity_field: entity}}, {"range": {time_field: {"gte": since.isoformat(), "lt": now.isoformat()}}}]
        if extra_filter:
            f.append(extra_filter)
        hits = self._hits({"size": 1, "_source": [time_field], "sort": [{time_field: "desc"}], "query": {"bool": {"filter": f}}}, index)
        if not hits:
            return None
        v = hits[0]["_source"].get(time_field)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None

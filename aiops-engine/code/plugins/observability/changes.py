"""IT 的「換版」：服務的版號（service.version）第一次出現的時間。換版後 deploy_grace 分鐘內的異常上限 warning（investigator/severity.py）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def deploy_at(es, service: str, since: datetime, now: datetime, index: str = "metrics-apm.transaction.1m-*",
              field: str = "service.version") -> datetime | None:
    """這個服務最近一次換版的時間：since 之後第一次出現的新版本（之前還有別的版本才算換版，不是服務剛上線）。查不到回 None。"""
    body = {"size": 0, "query": {"bool": {"filter": [{"term": {"service.name": service}},
                                                       {"range": {"@timestamp": {"gte": (since - timedelta(hours=24)).isoformat(), "lt": now.isoformat()}}}]}},
            "aggs": {"v": {"terms": {"field": field, "size": 20}, "aggs": {"first": {"min": {"field": "@timestamp"}}}}}}
    buckets = es.search(index, body).get("aggregations", {}).get("v", {}).get("buckets", [])
    firsts = sorted(datetime.fromtimestamp(b["first"]["value"] / 1000, tz=timezone.utc) for b in buckets if b.get("first", {}).get("value") is not None)
    new = [t for t in firsts[1:] if t >= since]     # 最早的那個版本不算
    return max(new) if new else None

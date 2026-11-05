"""工廠的「換版」：recipe 變更紀錄最近一筆的時間。變更後 deploy_grace 分鐘內的異常上限 warning（investigator/alerts/severity.py）。
IT 看 service.version 第一次出現（plugins/observability/changes.py）；工廠的變更紀錄一筆一次，直接拿最近一筆。"""
from __future__ import annotations

from datetime import datetime


def change_at(es, index: str, entity_field: str, entity: str, since: datetime, now: datetime, time_field: str = "@timestamp",
              extra_filter: dict | None = None) -> datetime | None:
    """這台機台 since 之後最近一筆變更（recipe 變更、設定變更）的時間；沒有回 None。index 是變更紀錄（一筆一次變更）。"""
    f = [{"term": {entity_field: entity}}, {"range": {time_field: {"gte": since.isoformat(), "lt": now.isoformat()}}}]
    if extra_filter:
        f.append(extra_filter)
    body = {"size": 1, "_source": [time_field], "sort": [{time_field: "desc"}], "query": {"bool": {"filter": f}}}
    hits = es.search(index, body).get("hits", {}).get("hits", [])
    if not hits:
        return None
    v = hits[0]["_source"].get(time_field)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None

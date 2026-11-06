"""工廠的「換版」：recipe 變更紀錄最近一筆的時間。變更後 deploy_grace 分鐘內的異常上限 warning（incident/alert_severity.py）。
IT 看 service.version 第一次出現（incident/strategy/it_deployment.py）；工廠的變更紀錄一筆一次，直接拿最近一筆。"""
from __future__ import annotations

from datetime import datetime


def change_at(repo, index: str, entity_field: str, entity: str, since: datetime, now: datetime, time_field: str = "@timestamp",
              extra_filter: dict | None = None) -> datetime | None:
    """這台機台 since 之後最近一筆變更（recipe 變更、設定變更）的時間；沒有回 None。index 是變更紀錄（一筆一次變更）。"""
    return repo.change.latest(index, entity_field, entity, since, now, time_field, extra_filter)


class LatestChange:
    """cfg 的 alerts.deploy_index／deploy_entity_field／deploy_time_field／deploy_filter 可改；預設 eqp-logs-* 的 recipe_change。"""

    def __call__(self, repo, cfg: dict, entity: str, since: datetime, now: datetime) -> datetime | None:
        acfg = cfg.get("alerts", {})
        return change_at(repo, acfg.get("deploy_index", "eqp-logs-*"), acfg.get("deploy_entity_field", "equipment_id"), entity,
                         since, now, acfg.get("deploy_time_field", "@timestamp"), acfg.get("deploy_filter", {"term": {"event_type": "recipe_change"}}))

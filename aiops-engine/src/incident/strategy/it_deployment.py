"""IT 的「換版」：服務的版號（service.version）第一次出現的時間。換版後 deploy_grace 分鐘內的異常上限 warning（incident/alert_severity.py）。"""
from __future__ import annotations

from incident.config.rca import RcaConfig
from datetime import datetime


def deploy_at(repo, service: str, since: datetime, now: datetime, index: str = "metrics-apm.transaction.1m-*",
              field: str = "service.version") -> datetime | None:
    """這個服務最近一次換版的時間：since 之後第一次出現的新版本（之前還有別的版本才算換版，不是服務剛上線）。查不到回 None。"""
    firsts = repo.change.version_first_seen(index, service, since, now, field)
    new = [t for t in firsts[1:] if t >= since]     # 最早的那個版本不算
    return max(new) if new else None


class VersionFirstSeen:
    """rca_config 的 alerts.deploy_index、alerts.version_field 可改查哪個索引、哪個欄位。"""

    def __call__(self, repo, rca_config: RcaConfig, entity: str, since: datetime, now: datetime) -> datetime | None:
        acfg = rca_config.alerts
        return deploy_at(repo, entity, since, now, acfg.deploy_index or "metrics-apm.transaction.1m-*", acfg.version_field or "service.version")

"""p0：P0 服務（rca.yaml 的 alerts.p0_services）上面算完再升一級（原本在 incident/alert_severity.py）。
換版後的上限 warning 也跟著變 high：P0 換版壞了也要叫人。門檻（算不算異常）不動。"""
from __future__ import annotations

import re

from incident.alert_severity import SeverityContext

UP = {"warning": "high", "high": "critical", "critical": "critical"}


def is_p0(service: str | None, patterns: list[str] | tuple[str, ...]) -> bool:
    """服務名完全等於、或整個名字對得上 regex（寫壞的 regex 當一般名字比）。"""
    if not service:
        return False
    for p in patterns or ():
        if p == service:
            return True
        try:
            if re.fullmatch(p, service):
                return True
        except re.error:
            continue
    return False


def escalate(g: dict, p0: bool) -> dict:
    """P0 服務：grade 的結果升一級，why 寫出來。不是 P0 原樣回。"""
    if not p0:
        return g
    up = UP[g["severity"]]
    why = g["why"] + ("；P0 服務，升一級" if up != g["severity"] else "；P0 服務")
    return {**g, "severity": up, "why": why, "p0": True}


def run(ctx: SeverityContext, g: dict | None) -> dict:
    """P0 服務（alerts.p0_services）：前一塊的結果升一級。前面沒有評過（g 是 None）就當 warning 再升。"""
    base = g or {"severity": "warning", "why": "沒有前一塊的評分"}
    return escalate(base, is_p0(getattr(ctx.alert, "service", None), getattr(ctx.alerts_config, "p0_services", None) or []))

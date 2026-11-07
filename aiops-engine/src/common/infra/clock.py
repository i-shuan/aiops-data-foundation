"""時間的小工具：設定檔的時間長度（30m、1h、7d）、「現在」（--at 回放）。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pandas as pd

# 開著的件、心跳的 @timestamp 往前寫這麼多（docs/pdca/phase3-locate/2026-10-alert-flap.md）。
# 一件只有一筆、每輪覆蓋；Grafana 的查詢範圍是「排定的評估時間往前 N 分鐘」，查詢卻晚幾秒才送出。
# 覆蓋剛好落在這幾秒之間時，@timestamp 比範圍的結束點還晚，Grafana 查到 0 筆、當成恢復（實測）。
# 往前寫 1 分鐘：主機間時鐘差、Grafana 的延遲都遠小於它；規則的窗是 5 分鐘（心跳 10 分鐘），還剩 4 分鐘的餘裕。
STAMP_LAG = timedelta(minutes=1)


def stamp_of(now: datetime) -> datetime:
    """開著的件、心跳寫進 aiops-alerts 的 @timestamp（Grafana 用它判斷「最近還有沒有」）。"""
    return now - STAMP_LAG


_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def duration(s: str) -> timedelta:
    """30m、1h、7d、2w → timedelta。"""
    m = re.fullmatch(r"(\d+)([smhdw])", str(s))
    if not m:
        raise SystemExit(f"時間長度看不懂：{s}（例如 30m、1h、7d）")
    return timedelta(**{_UNITS[m[2]]: int(m[1])})


def now_ts(arg: str | None) -> pd.Timestamp:
    """--at 給了就當成現在（回放），沒給就是現在（UTC）。"""
    return pd.Timestamp(arg) if arg else pd.Timestamp.now(tz="UTC")


def parse_ts(s: str | None) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else datetime.now(timezone.utc)

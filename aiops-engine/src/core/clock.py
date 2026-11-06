"""時間的小工具：設定檔的時間長度（30m、1h、7d）、「現在」（--at 回放）。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pandas as pd

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

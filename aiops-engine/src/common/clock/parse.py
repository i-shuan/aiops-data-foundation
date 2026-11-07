"""時間的解析：整個專案只有這一份。

- 設定檔的時間長度（`Dur`：30s、5m、1h、7d、2w）→ `parse_duration`；pandas 的頻率字串 → `pandas_freq`
- 命令列的「現在」（--at、--end）→ `parse_instant`：一律回帶時區的 pandas.Timestamp（沒寫時區當 UTC）
- 時區名稱 → `zone`（設定檔載入時就驗過，用到這裡不會再錯）

字串只在邊界解析：命令列在 interface/cli（typer 的 parser），設定檔在載入時（pydantic 驗型別）。往下傳的是
`pandas.Timestamp`、`pandas.Timedelta`、`TimeWindow`，不是字串。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

_UNITS = {"ms": 0.001, "s": 1, "m": 60, "min": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|min|m|h|d|w)")


def parse_duration(value) -> timedelta:
    """30m、1h、7d、2w（也收 30min 與數字＝秒）→ timedelta。看不懂丟 ValueError。"""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=value)
    m = _DURATION.fullmatch(str(value).strip().lower())     # 1D、30Min 也收（pandas 的寫法）
    if not m:
        raise ValueError(f"時間長度看不懂：{value}（例如 30m、1h、7d）")
    return timedelta(seconds=float(m[1]) * _UNITS[m[2]])


def pandas_freq(interval: str) -> str:
    """設定檔用 ES 的寫法（5m、1h），pandas 要寫成 5min、1h。"""
    return interval[:-1] + "min" if interval.endswith("m") else interval


def parse_instant(value: str) -> pd.Timestamp:
    """ISO 時間（2026-10-10T08:00:00Z、2026-10-10 08:00+08:00）→ 帶時區的 Timestamp；沒寫時區當 UTC。看不懂丟 ValueError。"""
    try:
        t = pd.Timestamp(value)
    except (ValueError, TypeError) as e:
        raise ValueError(f"時間看不懂：{value}（例如 2026-10-10T08:00:00Z）") from e
    if pd.isna(t):
        raise ValueError(f"時間看不懂：{value}")
    return t.tz_localize("UTC") if t.tzinfo is None else t


def check_zone(name: str) -> str:
    """設定檔的 timezone 欄位用（pydantic AfterValidator）：不認得的時區名稱在載入時就擋下來。"""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise ValueError(f"不認得的時區：{name}（例如 Asia/Taipei、UTC）") from e
    return name


def zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def stored_time(v) -> datetime | None:
    """存起來的文件裡的時間（ISO 字串、datetime、epoch 毫秒）→ 帶時區的 datetime；看不懂回 None（ts 欄空著，不擋寫入）。"""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

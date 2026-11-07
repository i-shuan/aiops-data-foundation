"""一段時間（[start, end)）與「現在」。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from common.clock.parse import parse_duration


def now_of(at: pd.Timestamp | None = None) -> pd.Timestamp:
    """--at 給了就當成現在（回放），沒給就是現在（UTC）。at 已經是 Timestamp（interface 解析過）。"""
    if at is not None and not isinstance(at, pd.Timestamp):
        raise TypeError(f"at 要是 pandas.Timestamp，收到 {type(at).__name__}：字串要在 interface 先用 parse_instant 解析")
    return at if at is not None else pd.Timestamp.now(tz="UTC")


@dataclass(frozen=True)
class TimeWindow:
    """[start, end)，兩端都是帶時區的 Timestamp。"""
    start: pd.Timestamp
    end: pd.Timestamp

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("TimeWindow 的兩端要帶時區")
        if self.end < self.start:
            raise ValueError(f"TimeWindow 結束（{self.end}）在開始（{self.start}）之前")

    @classmethod
    def ending(cls, end: pd.Timestamp, length) -> "TimeWindow":
        """到 end 為止、往前 length（Timedelta、timedelta 或 30m 這種字串）。"""
        return cls(end - pd.Timedelta(parse_duration(length) if isinstance(length, str) else length), end)

    @property
    def length(self) -> pd.Timedelta:
        return self.end - self.start

    def py(self) -> tuple[datetime, datetime]:
        return self.start.to_pydatetime(), self.end.to_pydatetime()

    def __contains__(self, t) -> bool:
        return self.start <= t < self.end


def show_duration(d: timedelta | pd.Timedelta) -> str:
    """Timedelta → 30m、1h、2d（印給人看；整除的最大單位）。"""
    s = int(d.total_seconds())
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if s and s % size == 0:
            return f"{s // size}{unit}"
    return f"{s}s"

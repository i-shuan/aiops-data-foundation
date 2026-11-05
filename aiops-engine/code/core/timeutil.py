"""時間、頻率、keys 的小工具（detector、investigator、plugin 都用）。"""
from __future__ import annotations

import pandas as pd


def to_freq(interval: str) -> str:
    """設定檔用 ES 的寫法（5m、1h），pandas 要寫成 5min、1h。"""
    return interval[:-1] + "min" if interval.endswith("m") else interval


_freq = to_freq


def keys_of(spec: dict) -> list[str]:
    """序列依什麼切：有 keys（統一的名字，例如 service、method、route）用 keys，否則就是 by 的欄位路徑。"""
    return spec.get("keys") or spec["by"]


def _now(arg: str | None) -> pd.Timestamp:
    return pd.Timestamp(arg) if arg else pd.Timestamp.now(tz="UTC")


def _v(x):
    """DataFrame 合併後空的欄位是 NaN，ES 不收：轉成 None。"""
    return None if isinstance(x, float) and pd.isna(x) else x

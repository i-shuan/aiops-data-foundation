"""detector 各指令共用的小工具（時間、頻率、keys、寫異常總表時補的欄位）。"""
from __future__ import annotations

import pandas as pd

from sources.schema import RULE_FIELDS  # noqa: F401  規則類寫異常時補的欄位，定義在 sources/schema.py；名字留給既有的 import
from .model import to_freq


def keys_of(spec: dict) -> list[str]:
    """序列依什麼切：有 keys（統一的名字，例如 service、method、route）用 keys，否則就是 by 的欄位路徑。"""
    return spec.get("keys") or spec["by"]

_freq = to_freq

def _now(arg: str | None) -> pd.Timestamp:
    return pd.Timestamp(arg) if arg else pd.Timestamp.now(tz="UTC")

def _v(x):
    """DataFrame 合併後空的欄位是 NaN，ES 不收：轉成 None。"""
    return None if isinstance(x, float) and pd.isna(x) else x

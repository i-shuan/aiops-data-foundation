"""detector 各指令共用的小工具（時間、頻率、keys、序列值的種類）。"""
from __future__ import annotations

import pandas as pd



def dflt(value, default):
    """設定欄位沒寫（None）就用 default。"""
    return default if value is None else value


def keys_of(spec) -> list[str]:
    """序列依什麼切：有 keys（統一的名字，例如 service、method、route）用 keys，否則就是 by 的欄位路徑。"""
    return spec.key_names or spec.by

def _v(x):
    """DataFrame 合併後空的欄位是 NaN，ES 不收：轉成 None。"""
    return None if isinstance(x, float) and pd.isna(x) else x


def value_kind(spec) -> str:
    """count：值本身就是事件數（筆數、請求數）；rate：比例或百分位，事件數是底下的請求數；
    gauge：量測值（CPU、記憶體），不看事件數。預先統計好的筆數（例如 {sum: requests}）設 events: value。"""
    events = getattr(spec, "events", None)
    if events == "value":
        return "count"
    if isinstance(events, dict):   # 預先統計好的資料：事件數在另一個欄位（例如 {sum: requests}）
        return getattr(spec, "kind", None) or "rate"
    value = spec.value
    if value == "count":
        return "count"
    (kind, _), = value.items()
    return {"hist_count": "count", "ratio": "rate", "hist_ratio": "rate", "hist_ratio_above": "rate", "percentile": "rate",
            "div": "rate"}.get(kind, "gauge")

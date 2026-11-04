"""M2 Pod 同儕橫向比對 (Peer Comparison)：同一個服務的 Pod 互相比，抓「只有一個 Pod 壞掉」。原理 detector/peer.py 檔頭。

輸入 peer：
  欄位   group… + member + value [+ volume]
  一列   一個 Pod（member）在這個 window 的一個值；group 是它屬於哪個服務（可以多欄：namespace、deployment）；
         volume 是這段時間的請求數（有給就能用 min_volume 擋流量太少的 Pod）
  例     ns     deploy    pod          value   volume
         mes    mes-api   mes-api-0    0.012   830

用法：
  r = compare(df, group=["ns", "deploy"], member="pod", direction="up", z=5, min_abs=0.05, min_ratio=3)
  r 每個 Pod 一列：group、member、value、peers（其他 Pod 幾個）、median（其他 Pod 的中位數）、z、anomaly
"""
from __future__ import annotations

import pandas as pd

from detector.peer import compare, describe  # noqa: F401

INPUT = ("value",)


def check_peer(df: pd.DataFrame, group: list[str], member: str) -> pd.DataFrame:
    missing = [c for c in list(group) + [member, "value"] if c not in df.columns]
    if missing:
        raise ValueError(f"peer 缺欄位 {missing}：要有 {list(group)} + [{member!r}, 'value']（有：{list(df.columns)}）")
    return df.assign(value=pd.to_numeric(df["value"], errors="coerce"))


def compare_checked(df: pd.DataFrame, group: list[str], member: str, *, min_volume: float | None = None, **kw) -> pd.DataFrame:
    """check_peer → （有 volume 欄位與 min_volume 就先擋流量太少的）→ compare。"""
    d = check_peer(df, group, member)
    if min_volume is not None and "volume" in d.columns:
        d = d[pd.to_numeric(d["volume"], errors="coerce").fillna(0) >= min_volume]
    return compare(d, list(group), member, **kw)

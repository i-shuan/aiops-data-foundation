"""跟兄弟比（同儕比較）：同一個服務的幾個 Pod 互相比，抓「只有一個 Pod 壞掉」（docs/metrics/README.md 的做法 4）。

為什麼要：10 個 Pod 裡 1 個錯誤率 100%，整個服務的錯誤率只有 10%，跟平常比（baseline）可能還在範圍裡；
記憶體只有一個 Pod 一直漲，平均起來也看不出來。同一個服務的 Pod 跑同一份程式、分到差不多的流量，
應該長得差不多，所以拿「其他 Pod」當標準，不用歷史，裝上去第一天就能用。

每個檢查（configs/baseline/peer.yaml）：
  1. 用 series.yaml 一樣的寫法查出每個 Pod 最近 window（例如 10 分鐘）的一個值（錯誤率、p95、CPU…）。
  2. 依 group（例如服務）分組，至少 min_peers 個 Pod 才比。
  3. 每個 Pod 跟「其他 Pod」比：其他 Pod 的中位數、MAD（跟 baseline 一樣的穩健統計，少數怪的不會把標準拉偏），
     離中位數 z 倍穩健標準差以上、而且差距 ≥ min_abs（以及 ≥ min_ratio 倍，若有設）才算。
     其他 Pod 只有 2～4 個時，它們之間的差距沒什麼統計意義（滾動更新後新舊 Pod 的記憶體本來就差幾十 MB，會把分母撐大），
     z 的門檻依其他 Pod 數分級（z_by_peers，例如其他 2 個用 3），主要靠 min_abs、min_ratio 擋誤報
     （docs/pdca/phase2-anomaly/2026-11-peer-z-and-slope.md）。
  流量太少的 Pod（min_volume，例如 10 分鐘不到 20 個請求）不比：錯誤率一兩個錯就跳很高。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .model import MAD_TO_SIGMA


def compare(df: pd.DataFrame, group: list[str], member: str, *, direction: str = "up", z: float = 5.0,
            min_abs: float = 0.0, min_ratio: float | None = None, rel_floor: float = 0.1, abs_floor: float = 1e-9,
            min_peers: int = 3, max_peer_median: float | None = None, z_by_peers: dict | None = None) -> pd.DataFrame:
    """df：group + [member, "value"]，每個 Pod 一列。

    回傳每個 Pod 一列：group、member、value、peers（其他 Pod 幾個）、median（其他 Pod 的中位數）、z、anomaly。
    rel_floor：其他 Pod 幾乎一樣時（MAD≈0），差距至少當成中位數的 rel_floor 倍，避免一點點不同就算。
    z_by_peers：{其他 Pod 數: z 門檻}，沒列的用 z（例如 {2: 3, 3: 4, 4: 4}：其他只有 2 個時 3 倍就算）。
    max_peer_median：其他 Pod 的中位數已經到這個值（例如錯誤率 5%）＝整個服務都壞了，不挑出單一個 Pod，交給規則。
    """
    cols = group + [member, "value", "peers", "median", "z", "anomaly"]
    d = df.dropna(subset=["value"])
    rows = []
    for _, g in d.groupby(group, sort=False, dropna=False):
        if len(g) < max(min_peers, 2):
            continue
        vals = g["value"].to_numpy(dtype=float)
        zt = float((z_by_peers or {}).get(len(vals) - 1, z))   # 其他 Pod 數 = 這組減自己
        for i, r in enumerate(g.to_dict("records")):
            others = np.delete(vals, i)
            med = float(np.median(others))
            mad = float(np.median(np.abs(others - med)))
            sigma = max(MAD_TO_SIGMA * mad, rel_floor * abs(med), abs_floor)
            diff = r["value"] - med
            zz = diff / sigma
            if max_peer_median is not None and direction == "up" and med >= max_peer_median:
                hit = False
            elif direction == "up":
                hit = zz >= zt and diff >= min_abs and (min_ratio is None or r["value"] >= min_ratio * med)
            elif direction == "down":
                hit = zz <= -zt and -diff >= min_abs and (min_ratio is None or r["value"] * min_ratio <= med)
            else:
                hit = abs(zz) >= zt and abs(diff) >= min_abs
            rows.append({**{k: r[k] for k in group}, member: r[member], "value": r["value"],
                         "peers": len(others), "median": med, "z": zz, "anomaly": bool(hit)})
    return pd.DataFrame(rows, columns=cols)


def volume_spec(spec: dict) -> dict | None:
    """錯誤率、p95 這種從直方圖算的，另外查每個 Pod 的請求數（同一個直方圖的次數），用來排除流量太少的 Pod。"""
    def one(v: dict) -> dict | None:
        (kind, arg), = v["value"].items() if isinstance(v["value"], dict) else ((None, None),)
        if kind in ("hist_ratio", "percentile"):
            return {**v, "value": {"hist_count": arg["field"]}}
        return None
    return one(spec)


def count_spec(spec: dict) -> dict:
    """同一個查詢、只數筆數：看哪些 Pod 最近還有資料。"""
    return {**spec, "value": "count"}


def describe(check: dict, r: dict, keys: list[str], fmt) -> str:
    who = "、".join(str(r[k]) for k in keys if r.get(k) is not None)
    return (f"{check['title']}：{who}（{fmt(r['value'])}，其他 {r['peers']} 個 Pod 中位數 {fmt(r['median'])}，"
            f"z={'>999' if abs(r['z']) > 999 else format(r['z'], '.1f')}）")   # 其他 Pod 都是 0 時 z 會上億，沒意義

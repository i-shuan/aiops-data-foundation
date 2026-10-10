"""學「平常的樣子」（baseline）與判斷異常。不碰 ES，輸入輸出都是 pandas DataFrame。

方法（docs/aiops-rca.md 的方法 A）：
  每條時間序列、每個時段（星期幾 × 幾點），取過去幾週同一時段的值，算中位數與中位數絕對偏差（MAD）。
  之後的值離中位數超過 z 倍「穩健標準差」（1.4826 × MAD）就算異常。中位數與 MAD 不受少數極端值影響，
  歷史裡混了幾次事故也不太會把「平常」拉偏。

歷史不夠時往下退（level 欄位記錄用了哪一層，可信度依序降低）：
  dow_hour：同一個星期幾、同一個小時，至少 min_weeks 個不同的日子
  hour    ：每天同一個小時，至少 min_days 個不同的日子
  all     ：不分時段，至少 min_points 個點
都不夠就沒有 baseline，這條序列不判斷。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MAD_TO_SIGMA = 1.4826  # 常態分布下 MAD 換成標準差的係數

LEVELS = ("dow_hour", "hour", "all")


@dataclass(frozen=True)
class TrainOptions:
    timezone: str = "Asia/Taipei"  # 週期依工廠的當地時間算（換班、上下班）
    min_weeks: int = 2             # dow_hour：同一個星期幾至少要有幾天
    min_days: int = 3              # hour：同一個小時至少要有幾天
    min_points: int = 50           # all：至少要有幾個點


@dataclass(frozen=True)
class DetectOptions:
    z: float = 5.0            # 離中位數幾倍穩健標準差算異常
    rel_floor: float = 0.05   # 平常幾乎不動的序列（MAD 接近 0），波動至少當成中位數的 5%，避免一點點變化就告警
    abs_floor: float = 1e-9   # 中位數也是 0 時的最小波動


def _slots(ts: pd.Series, tz: str) -> pd.DataFrame:
    local = pd.to_datetime(ts, utc=True).dt.tz_convert(tz)
    return pd.DataFrame({"dow": local.dt.dayofweek, "hour": local.dt.hour, "day": local.dt.date})


def _stats(values: pd.Series) -> dict:
    v = values.to_numpy(dtype=float)
    med = float(np.median(v))
    return {
        "median": med,
        "mad": float(np.median(np.abs(v - med))),
        "p05": float(np.percentile(v, 5)),
        "p95": float(np.percentile(v, 95)),
        "n": int(v.size),
    }


def train(history: pd.DataFrame, keys: list[str], opts: TrainOptions = TrainOptions()) -> pd.DataFrame:
    """history：欄位 keys + ["ts", "value"]，每列是一條序列的一個點（value 為 NaN 的點不算）。

    回傳每條序列、每個時段（dow 0=週一 … 6、hour 0～23）一列：keys + dow、hour、level、median、mad、p05、p95、n。
    每個時段各自從 dow_hour → hour → all 往下找第一個樣本夠的層級。
    """
    h = history.dropna(subset=["value"]).copy()
    if h.empty:
        return pd.DataFrame(columns=keys + ["dow", "hour", "level", "median", "mad", "p05", "p95", "n"])
    h = pd.concat([h.reset_index(drop=True), _slots(h["ts"], opts.timezone).reset_index(drop=True)], axis=1)

    rows = []
    for key_vals, g in h.groupby(keys, sort=False, dropna=False):
        key_vals = key_vals if isinstance(key_vals, tuple) else (key_vals,)
        key_dict = dict(zip(keys, key_vals))
        all_stats = _stats(g["value"]) if len(g) >= opts.min_points else None
        by_hour = {hr: gg for hr, gg in g.groupby("hour")}
        by_dow_hour = {k: gg for k, gg in g.groupby(["dow", "hour"])}
        for dow in range(7):
            for hour in range(24):
                gg = by_dow_hour.get((dow, hour))
                if gg is not None and gg["day"].nunique() >= opts.min_weeks:
                    rows.append({**key_dict, "dow": dow, "hour": hour, "level": "dow_hour", **_stats(gg["value"])})
                    continue
                gg = by_hour.get(hour)
                if gg is not None and gg["day"].nunique() >= opts.min_days:
                    rows.append({**key_dict, "dow": dow, "hour": hour, "level": "hour", **_stats(gg["value"])})
                    continue
                if all_stats is not None:
                    rows.append({**key_dict, "dow": dow, "hour": hour, "level": "all", **all_stats})
    return pd.DataFrame(rows)


def score(current: pd.DataFrame, baseline: pd.DataFrame, keys: list[str], direction: str = "both",
          min_abs: float = 0.0, opts: DetectOptions = DetectOptions(), tz: str = "Asia/Taipei") -> pd.DataFrame:
    """current：keys + ["ts", "value"]。對每個點找它那個時段的 baseline，算 z 值。

    direction：up（只有變高算異常，例如延遲、錯誤數）、down（只有變低）、both。
    min_abs：跟中位數至少要差這麼多才算（避免量很小的序列，從 1 變 3 就告警）。
    回傳 current 加上 level、median、mad、expected_low、expected_high、z、anomaly；沒有 baseline 的點 anomaly 為 False。
    """
    c = current.dropna(subset=["value"]).copy().reset_index(drop=True)
    if c.empty:
        return c.assign(level=None, median=np.nan, mad=np.nan, expected_low=np.nan, expected_high=np.nan,
                        z=np.nan, anomaly=False)
    slots = _slots(c["ts"], tz)
    c["dow"], c["hour"] = slots["dow"].to_numpy(), slots["hour"].to_numpy()
    b = baseline[keys + ["dow", "hour", "level", "median", "mad"]]
    m = c.merge(b, on=keys + ["dow", "hour"], how="left")

    sigma = np.maximum.reduce([
        MAD_TO_SIGMA * m["mad"].to_numpy(dtype=float),
        opts.rel_floor * np.abs(m["median"].to_numpy(dtype=float)),
        np.full(len(m), opts.abs_floor),
    ])
    diff = m["value"].to_numpy(dtype=float) - m["median"].to_numpy(dtype=float)
    m["z"] = diff / sigma
    m["expected_low"] = m["median"] - opts.z * sigma
    m["expected_high"] = m["median"] + opts.z * sigma
    over = np.abs(diff) >= min_abs
    if direction == "up":
        hit = (m["z"] >= opts.z) & over
    elif direction == "down":
        hit = (m["z"] <= -opts.z) & over
    else:
        hit = (m["z"].abs() >= opts.z) & over
    m["anomaly"] = hit.fillna(False) & m["median"].notna()
    return m.drop(columns=["dow", "hour"])

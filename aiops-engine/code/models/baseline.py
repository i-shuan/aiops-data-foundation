"""M1 動態基準線學習 (Dynamic Baseline Learning)：學「平常的樣子」、拿現在跟平常比。原理 docs/rca/README.md。

輸入 series（history、current 都是）：
  欄位   keys… + ts + value + n
  一列   一條序列在一個時間窗的值。keys 是序列的識別（例如 service、route）；ts 是時間窗的開始（UTC）；
         value 是這個窗的值（請求數、錯誤率、p95 秒…）；n 是底下的事件數（請求數），量測值（CPU）是 NaN
  例     service   route           ts                         value   n
         mes-api   GET /lots/{id}  2026-11-02 12:00:00+00:00  0.031   412

用法：
  b = train(history, keys=["service", "route"])                      # keys + dow、hour、level、median、mad…
  s = score(current, b, keys=["service", "route"], direction="up")   # current 加 z、expected_low／high、anomaly
  s = mark_status(s, keys, step="1m", confirm_points=2)              # anomaly／warning
  稀少的筆數：train(…, method="poisson") 搭 score_poisson。
  粒度：pick_resolution(volume, keys, min_events) 從 keys + ts + n 挑每條序列用多粗的時間窗；mask_thin 把事件數太少的窗當不知道。
"""
from __future__ import annotations

import pandas as pd

from detector.model import (CONFIDENCE, LEVELS, RESOLUTIONS, DetectOptions, TrainOptions, mark_status, mask_thin,  # noqa: F401
                            pick_resolution, score, score_poisson, train, train_groups)

INPUT = ("ts", "value", "n")


def check_series(df: pd.DataFrame, keys: list[str], what: str = "series") -> pd.DataFrame:
    """欄位齊不齊、ts 是不是帶時區的時間。回傳整理過的 df（ts 轉 UTC、value／n 轉數字）。"""
    missing = [c for c in list(keys) + list(INPUT) if c not in df.columns]
    if missing:
        raise ValueError(f"{what} 缺欄位 {missing}：要有 {list(keys)} + {list(INPUT)}（有：{list(df.columns)}）")
    out = df.copy()
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out["n"] = pd.to_numeric(out["n"], errors="coerce")
    return out


def fit_and_score(history: pd.DataFrame, current: pd.DataFrame, keys: list[str], *, direction: str = "both",
                  min_abs: float = 0.0, train_opts: TrainOptions = TrainOptions(), detect_opts: DetectOptions = DetectOptions(),
                  method: str = "mad", step: str | None = None, confirm_points: int = 2) -> tuple[pd.DataFrame, pd.DataFrame]:
    """一次做完：學 history → 給 current 打分（→ step 有給就標 status）。回傳 (baseline, scored)。
    指令（detector train／detect）做的事比這多：挑粒度、借組 baseline、排除時段、tuning 的 override；這裡是最小的一條路。"""
    h, c = check_series(history, keys, "history"), check_series(current, keys, "current")
    b = train(h, keys, train_opts, method=method)
    if b.empty:
        return b, c.assign(anomaly=False)
    fn = score_poisson if method == "poisson" else score
    s = fn(c, b, keys, direction=direction, min_abs=min_abs, opts=detect_opts, tz=train_opts.timezone)
    if step:
        s = mark_status(s, keys, step, confirm_points)
    return b, s

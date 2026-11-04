"""M3 靜態臨界值規則 (Static Threshold Rules)：不用學、不用歷史，裝上去第一天就有用。規則的寫法見 detector/rules.py 檔頭。

輸入 points（value／compare／ratio／increase／delta／increase_ratio／rate／total／slope 型）：
  {by 的值元組: [(ts, 文件), …]}   一個對象、依時間排好的原始文件；文件是 dict，欄位名是點號路徑
  （regular 的原始列 DataFrame 用 points_of 轉：一列一個時間點，欄位名就是規則裡寫的 field、by、den…）
輸入 series（window 型：整段算一個值）：
  by… + value + n   每個對象一列（n 是事件數，min_events 用）

用法：
  rule = {"name": "pod.restarts", "type": "increase", "field": "metrics.restarts", "by": ["pod"], "op": ">=", "threshold": 1}
  hits = evaluate(rule, points_of(df, rule, start, end), now)        # [{key, value, since, last}, …]
  hits = evaluate_window(rule, series_df, since)
"""
from __future__ import annotations

import pandas as pd

from detector.rules import OPS, describe, evaluate, evaluate_window  # noqa: F401
from sources.shape import points_from_rows


def points_of(rows: pd.DataFrame, rule: dict, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """原始列（一列一個時間點，時間欄位預設 @timestamp）→ points。"""
    return points_from_rows(rows, rule, start, end)


def check_rule(rule: dict) -> dict:
    for k in ("name", "type", "by"):
        if k not in rule:
            raise ValueError(f"規則缺 {k}：{rule}")
    if rule["type"] != "window" and "field" not in rule:
        raise ValueError(f"規則 {rule['name']} 缺 field")
    if "threshold" not in rule:
        raise ValueError(f"規則 {rule['name']} 缺 threshold")
    return rule

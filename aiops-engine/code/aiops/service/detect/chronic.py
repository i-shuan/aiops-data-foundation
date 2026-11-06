"""慢性病清單：天天噴錯、沒人修的（docs/pdca/phase2-anomaly/2026-10-chronic-calendar.md）。

為什麼要：baseline 跟自己的歷史比，天天都有的錯會被學成「平常」，永遠不報；真的出事時，平常值已經被墊高，
反而更難抓。這種問題不是調門檻能解的，要列出來讓人修。修掉的下週自然從清單消失。

兩種來源（configs/baseline/chronic.yaml）：
  type: log        錯誤 log（依服務 × error.type）：最近 days 天裡至少 min_days 天有、每天至少 min_per_day 筆
  type: anomalies  aiops-anomalies 裡的規則、跟兄弟比（N+1、吞掉的例外、CPU 被限流、只有一個 Pod 壞…）：
                   同一條、同一個對象，最近 days 天裡至少 min_days 天有觸發

每個來源排前 top 名（天數多的先，再看量），附負責人（owners，依 match 找）。結果寫 aiops-chronic，並印成報告。

find_chronic、owner_of 只做計算（單元測試用）；fetch_* 是查詢。
"""
from __future__ import annotations

import json

import pandas as pd


from aiops.service.detect.adjustments import match_keys


def find_chronic(daily: pd.DataFrame, keys: list[str], *, min_days: int = 6, min_per_day: float = 1,
                 top: int = 10) -> pd.DataFrame:
    """daily：keys + ["day", "count"]，每個對象每天一列（沒有的那天可以不給）。

    回傳慢性病（keys + days、total、per_day＝有出現那幾天的中位數、first_day、last_day），排前 top 名：
    出現天數多的先，同天數看 total。每天至少 min_per_day 筆才算那天「有」。
    """
    cols = keys + ["days", "total", "per_day", "first_day", "last_day"]
    d = daily[daily["count"] >= min_per_day]
    if d.empty:
        return pd.DataFrame(columns=cols)
    g = d.groupby(keys, dropna=False)
    out = pd.DataFrame({"days": g["day"].nunique(), "total": g["count"].sum(), "per_day": g["count"].median(),
                        "first_day": g["day"].min(), "last_day": g["day"].max()}).reset_index()
    out = out[out["days"] >= min_days]
    return out.sort_values(["days", "total"], ascending=False).head(top)[cols].reset_index(drop=True)


def owner_of(key: dict, owners: list[dict]) -> str | None:
    """owners：[{match: {service: "^mes-"}, owner: "MES 團隊"}, …]，第一個符合的（欄位可用別名）。"""
    for o in owners or []:
        if match_keys(key, o.get("match")):
            return o["owner"]
    return None


def describe(source: dict, row: dict, keys: list[str], days: int) -> str:
    who = "、".join("（無）" if pd.isna(row[k]) else str(row[k]) for k in keys)   # None、NaN 都是「無」
    return (f"{source.get('title', source['name'])}：{who}，{days} 天裡 {int(row['days'])} 天有，"
            f"有的那幾天每天約 {row['per_day']:.0f} 筆，共 {int(row['total'])} 筆")

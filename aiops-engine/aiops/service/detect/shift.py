"""shift：一階跳變當輔助——哪條序列最近一天幾乎每個點都往同一邊超標、而且新水準很穩，就提一筆 reset_point
（status proposed，人 adjust approve 才生效；docs/pdca/phase2-anomaly/2026-11-baseline-more-series.md）。

換版後延遲停在新水準時，中位數要約 2 週才跟上，這段期間每個點都報；reset_point 讓 train 從那一刻重新學。
ES 的 change_point 聚合要付費授權，這裡只看 aiops-anomalies 裡已經判出來的點：
  - 最近 window（預設 24 小時）這條序列、這個對象至少 min_points 個異常點（anomaly 或 warning 都算）
  - 至少 min_share（預設 0.9）往同一個方向
  - 新水準很穩：這些點的值的 MAD ÷ |值的中位數 − 平常值| ≤ max_spread（預設 0.3）——跳上去就停在那裡，不是一直在跳
符合就提 reset_point：reset_at 是第一個異常點的時間，warmup 用預設。
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from aiops.core.connection import ES, doc_id
from aiops.core.repository import Repos
from aiops.scenario import load_scenario
import aiops.service.detect.adjustments as A
from aiops.service.detect._util import _now
from aiops.service.detect.adjustments import load_adjustments

DEFAULTS = {"window": "24h", "min_points": 20, "min_share": 0.9, "max_spread": 0.3}


def find_shifts(anoms: pd.DataFrame, *, min_points: int = 20, min_share: float = 0.9, max_spread: float = 0.3) -> list[dict]:
    """anoms：series、target（keys 的 JSON）、ts、value、median、direction（up／down，依值 vs 平常算）。
    回傳每個符合的 (series, target)：{"series", "keys", "since", "direction", "points", "level", "usual", "share"}。"""
    out = []
    if anoms.empty:
        return out
    for (series, target), g in anoms.groupby(["series", "target"], sort=False):
        g = g.dropna(subset=["value", "median"])
        if len(g) < min_points:
            continue
        up = (g["value"] > g["median"]).mean()
        share, direction = (up, "up") if up >= 0.5 else (1 - up, "down")
        if share < min_share:
            continue
        v = g["value"].to_numpy(dtype=float)
        level, usual = float(np.median(v)), float(np.median(g["median"].to_numpy(dtype=float)))
        gap = abs(level - usual)
        if gap <= 0:
            continue
        spread = float(np.median(np.abs(v - level))) / gap
        if spread > max_spread:
            continue
        out.append({"series": series, "keys": json.loads(target), "since": pd.Timestamp(g["ts"].min()), "direction": direction,
                    "points": int(len(g)), "level": level, "usual": usual, "share": float(share), "spread": spread})
    return out


def proposal(s: dict, tz: str) -> dict:
    keys = s["keys"]
    since = s["since"].tz_convert(tz) if s["since"].tzinfo else s["since"].tz_localize("UTC").tz_convert(tz)
    return {"kind": "reset_point", "series": re.escape(s["series"]), "match": {k: f"^{re.escape(str(v))}$" for k, v in keys.items()},
            "reset_at": since.isoformat(), "status": "proposed", "source": "shift",
            "reason": f"一階跳變：最近 {s['points']} 個點有 {s['share']:.0%} 往{'上' if s['direction'] == 'up' else '下'}超標，"
                      f"新水準 {s['level']:.4g}（平常 {s['usual']:.4g}）很穩（離散 {s['spread']:.0%}）；"
                      f"像是換版後停在新位置，建議從 {since:%m-%d %H:%M} 重新學。核准前先確認那時候有沒有換版或調整"}


def _rows(repo: Repos, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """動態基準線判的異常（有 median、不是規則）：series、target、ts、value、median。"""
    rows = [{"series": s["series"], "target": json.dumps(s.get("keys") or {}, sort_keys=True, ensure_ascii=False),
             "ts": pd.Timestamp(s["@timestamp"]), "value": s.get("value"), "median": s.get("median")}
            for s in repo.anomalies.between(start, end, ["@timestamp", "series", "keys", "value", "median", "status", "level"], with_median=True)
            if s.get("level") != "rule"]
    return pd.DataFrame(rows, columns=["series", "target", "ts", "value", "median"])


def cmd_shift(cfg: dict, args) -> int:
    repo = Repos(ES(), load_scenario())
    repo.tuning.ensure()
    opts = {**DEFAULTS, **(cfg.get("shift") or {})}
    tz = cfg.get("train", {}).get("timezone", cfg.get("timezone", "Asia/Taipei"))
    now = _now(args.at)
    start = now - pd.Timedelta(opts["window"])
    found = find_shifts(_rows(repo, start, now), min_points=int(opts["min_points"]), min_share=float(opts["min_share"]),
                        max_spread=float(opts["max_spread"]))
    existing = {(a.get("series"), json.dumps(a.get("match"), sort_keys=True)) for a in load_adjustments(repo, cfg)
                if a.get("kind") == "reset_point"}
    n = 0
    for s in found:
        doc = proposal(s, tz)
        if (doc["series"], json.dumps(doc["match"], sort_keys=True)) in existing:
            print(f"  已經有 reset_point：{s['series']} {s['keys']}")
            continue
        _id = doc_id("shift", doc["series"], doc["match"])
        if repo.tuning.exists(_id):
            print(f"  建議已經提過：{s['series']} {s['keys']}（{_id}）")
            continue
        doc.update({"at": now.isoformat(), "by": "shift"})
        repo.tuning.put(_id, doc)
        print(f"  建議 {_id}：{A.describe(doc)}")
        n += 1
    print(f"{now.tz_convert(tz):%m-%d %H:%M}：看最近 {opts['window']}，{len(found)} 條序列像一階跳變，提了 {n} 筆 reset_point（proposed，adjust approve 才生效）")
    return 0

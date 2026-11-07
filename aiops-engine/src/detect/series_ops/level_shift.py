"""shift：一階跳變當輔助——哪條序列最近一天幾乎每個點都往同一邊超標、而且新水準很穩，就提一筆 reset_point
（status proposed，人 adjust approve 才生效；docs/pdca/phase2-anomaly/2026-11-baseline-more-series.md）。

換版後延遲停在新水準時，中位數要約 2 週才跟上，這段期間每個點都報；reset_point 讓 train 從那一刻重新學。
ES 的 change_point 聚合要付費授權，這裡只看 aiops-anomalies 裡已經判出來的點：
  - 最近 window（預設 24 小時）這條序列、這個對象至少 min_points 個異常點（anomaly 或 warning 都算）
  - 至少 min_share（預設 0.9）往同一個方向
  - 新水準很穩：這些點的值的 MAD ÷ |值的中位數 − 平常值| ≤ max_spread（預設 0.3）——跳上去就停在那裡，不是一直在跳
符合就提 reset_point：reset_at 是第一個異常點的時間，warmup 用預設。

只判往上的序列（direction: up，延遲、錯誤）往下掉時，往下的點不會判成異常、不在 aiops-anomalies，上面看不到；
可是中位數要約 2 週才跟上，這段期間退回舊的高水準會被當成正常。所以 quiet_drops 對這些序列另外用「往下」打一次分
（只在記憶體，不寫異常、不通知），一樣過上面的條件，提 reset_point（docs/pdca/meta/2026-11-cases-tier-drop.md）。
"""
from __future__ import annotations

from common.clock import now_of
from detect.config.series import SeriesConfig
import json
import re

import numpy as np
import pandas as pd

from common.infra.es import ES, doc_id
from common.infra.repos import Repos
from common.scenario import load_scenario
import detect.series_ops.manual_adjust as A
from detect.detect_service import detect_series
from detect.series_ops.manual_adjust import data_adjustments, load_overrides
from detect.strategy.dynamic_baseline import DetectOptions
from detect.utils import keys_of
from detect.series_ops.manual_adjust import load_adjustments

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
                    "points": int(len(g)), "level": level, "usual": usual, "share": float(share), "spread": spread,
                    "quiet": bool(g["quiet"].fillna(False).astype(bool).any()) if "quiet" in g else False})
    return out


def proposal(s: dict, tz: str) -> dict:
    keys = s["keys"]
    since = s["since"].tz_convert(tz) if s["since"].tzinfo else s["since"].tz_localize("UTC").tz_convert(tz)
    return {"kind": "reset_point", "series": re.escape(s["series"]), "match": {k: f"^{re.escape(str(v))}$" for k, v in keys.items()},
            "reset_at": since.isoformat(), "status": "proposed", "source": "shift",
            "reason": f"一階跳變：最近 {s['points']} 個點有 {s['share']:.0%} 往{'上' if s['direction'] == 'up' else '下'}超標，"
                      f"新水準 {s['level']:.4g}（平常 {s['usual']:.4g}）很穩（離散 {s['spread']:.0%}）；"
                      f"像是換版後停在新位置，建議從 {since:%m-%d %H:%M} 重新學。核准前先確認那時候有沒有換版或調整"
                      + ("（這條只判往上，往下掉不會通知；不重新學的話，退回舊水準要約 2 週後才報得出來）" if s.get("quiet") else "")}


def _rows(repo: Repos, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """動態基準線判的異常（有 median、不是規則）：series、target、ts、value、median。"""
    rows = [{"series": s["series"], "target": json.dumps(s.get("keys") or {}, sort_keys=True, ensure_ascii=False),
             "ts": pd.Timestamp(s["@timestamp"]), "value": s.get("value"), "median": s.get("median")}
            for s in repo.anomalies.between(start, end, ["@timestamp", "series", "keys", "value", "median", "status", "level"], with_median=True)
            if s.get("level") != "rule"]
    return pd.DataFrame(rows, columns=["series", "target", "ts", "value", "median"])


def quiet_drops(repo: Repos, series_config: SeriesConfig, now: pd.Timestamp, window: pd.Timedelta, tz: str) -> pd.DataFrame:
    """只判往上的序列，最近 window 往下超標的點（series、target、ts、value、median）：用 detect 同一套（粒度、調整、排除時段）、
    方向換成 down 打分；不寫 ES。沒有 baseline 的序列跳過。"""
    dopts = DetectOptions(**series_config.detect.model_dump(exclude_none=True))
    overrides, adjs = load_overrides(repo, series_config, now), data_adjustments(repo, series_config, now)
    rows = []
    for spec in series_config.series:
        if spec.direction != "up":
            continue
        keys = keys_of(spec)
        base = repo.baseline.load(spec.name, keys)
        if base.empty:
            continue
        down = spec.model_copy(update={"direction": "down"})
        for _, s in detect_series(repo, series_config, down, base, now, window, dopts, tz, overrides, adjs, quiet=True):
            if s.empty or "status" not in s:
                continue
            for r in s[s["status"].isin(["anomaly", "warning"])].to_dict("records"):
                rows.append({"series": spec.name, "target": json.dumps({k: r[k] for k in keys}, sort_keys=True, ensure_ascii=False),
                             "ts": pd.Timestamp(r["ts"]), "value": r.get("value"), "median": r.get("median"), "quiet": True})
    return pd.DataFrame(rows, columns=["series", "target", "ts", "value", "median", "quiet"])


def cmd_shift(series_config: SeriesConfig, args, repo: Repos | None = None) -> int:
    repo = repo or Repos(ES(), load_scenario())
    repo.tuning.ensure()
    opts = {**DEFAULTS, **(series_config.shift.model_dump(exclude_none=True) if series_config.shift else {})}
    tz = series_config.train.timezone or series_config.timezone or "Asia/Taipei"
    now = now_of(args.at)
    start = now - pd.Timedelta(opts["window"])
    drops = quiet_drops(repo, series_config, now, pd.Timedelta(opts["window"]), tz)
    if not drops.empty:
        print(f"  只判往上的序列：{drops.groupby(['series', 'target']).ngroups} 個對象最近往下超標（不通知，只看要不要重新學）")
    rows = _rows(repo, start, now)
    found = find_shifts(pd.concat([rows, drops], ignore_index=True) if not drops.empty else rows, min_points=int(opts["min_points"]),
                        min_share=float(opts["min_share"]), max_spread=float(opts["max_spread"]))
    existing = {(a.get("series"), json.dumps(a.get("match"), sort_keys=True)) for a in load_adjustments(repo, series_config)
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

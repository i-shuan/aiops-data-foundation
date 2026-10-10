"""用法（在 aiops/baseline 底下）：

  python3 -m aiops_baseline train  --config series.yaml [--weeks 4] [--end 2026-10-10T00:00:00Z]
  python3 -m aiops_baseline detect --config series.yaml [--window 30m] [--at 2026-10-10T08:00:00Z]

train ：讀過去 N 週的資料，每條序列、每個時段學出中位數與波動範圍，寫進 ES 的 aiops-baseline。
detect：讀最近一段時間的資料，跟 baseline 比，超出範圍的寫進 aiops-anomalies，並印出來。
--end、--at 用來回測（拿過去某個時間點當成「現在」）；不給就是現在。
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd
import yaml

from .es import (ANOMALY_INDEX, ANOMALY_MAPPING, BASELINE_INDEX, BASELINE_MAPPING, ES, doc_id, fetch,
                 load_baseline)
from .model import DetectOptions, TrainOptions, score, train


def _freq(interval: str) -> str:
    """設定檔用 ES 的寫法（5m、1h），pandas 要寫成 5min、1h。"""
    return interval[:-1] + "min" if interval.endswith("m") else interval


def _now(arg: str | None) -> pd.Timestamp:
    return pd.Timestamp(arg) if arg else pd.Timestamp.now(tz="UTC")


def cmd_train(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(BASELINE_INDEX, BASELINE_MAPPING)
    interval = cfg.get("interval", "5m")
    end = _now(args.end).floor(_freq(interval))
    start = end - pd.Timedelta(weeks=args.weeks or cfg.get("history_weeks", 4))
    topts = TrainOptions(**cfg.get("train", {}))
    trained_at = pd.Timestamp.now(tz="UTC").isoformat()
    print(f"學習區間 {start.tz_convert(topts.timezone):%Y-%m-%d %H:%M} ～ {end.tz_convert(topts.timezone):%Y-%m-%d %H:%M}"
          f"（{topts.timezone}，{interval} 一點）")
    for spec in cfg["series"]:
        hist = fetch(es, spec, start, end, interval)
        b = train(hist, spec["by"], topts)
        docs = []
        for r in b.to_dict("records"):
            keys = {f: r[f] for f in spec["by"]}
            docs.append((doc_id(spec["name"], keys, r["dow"], r["hour"]), {
                "series": spec["name"], "keys": keys, "dow": r["dow"], "hour": r["hour"], "level": r["level"],
                "median": r["median"], "mad": r["mad"], "p05": r["p05"], "p95": r["p95"], "n": r["n"],
                "trained_at": trained_at, "history_from": start.isoformat(), "history_to": end.isoformat()}))
        es.bulk_index(BASELINE_INDEX, docs)
        n_series = hist.groupby(spec["by"]).ngroups if not hist.empty else 0
        n_points = int(hist["value"].notna().sum()) if not hist.empty else 0
        levels = b["level"].value_counts().to_dict() if not b.empty else {}
        note = "（資料不夠，先不判斷）" if n_series and b.empty else ""
        print(f"  {spec['name']}：{n_series} 條序列、{n_points} 個有效的點 → {len(b)} 個時段的 baseline {levels}{note}")
    return 0


def _add_silent(cur: pd.DataFrame, base: pd.DataFrame, by: list[str], start, end, interval) -> pd.DataFrame:
    """有 baseline、但這段時間完全沒資料的序列補 0：服務整個停掉、不再有請求或 log，也要抓得到。"""
    if base.empty:
        return cur
    known = base[by].drop_duplicates()
    seen = cur[by].drop_duplicates() if not cur.empty else known.iloc[0:0]
    silent = known.merge(seen, on=by, how="left", indicator=True)
    silent = silent[silent["_merge"] == "left_only"][by]
    if silent.empty:
        return cur
    grid = pd.date_range(start, end, freq=_freq(interval), inclusive="left")
    zeros = silent.merge(pd.DataFrame({"ts": grid}), how="cross").assign(value=0.0)
    return pd.concat([cur, zeros[by + ["ts", "value"]]], ignore_index=True)


def cmd_detect(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    interval = cfg.get("interval", "5m")
    # 最後一段可能還沒收齊（筆數會偏低，看起來像「驟降」），所以只看已經結束的時段
    end = _now(args.at).floor(_freq(interval))
    start = end - pd.Timedelta(args.window)
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    dopts = DetectOptions(**cfg.get("detect", {}))
    detected_at = pd.Timestamp.now(tz="UTC").isoformat()
    total = 0
    print(f"偵測區間 {start.tz_convert(tz):%Y-%m-%d %H:%M} ～ {end.tz_convert(tz):%Y-%m-%d %H:%M}（{tz}）")
    for spec in cfg["series"]:
        cur = fetch(es, spec, start, end, interval)
        base = load_baseline(es, spec["name"], spec["by"])
        if spec["value"] == "count" or spec.get("fill_zero"):
            cur = _add_silent(cur, base, spec["by"], start, end, interval)
        if cur.empty or base.empty:
            print(f"  {spec['name']}：{'沒有資料' if cur.empty else '沒有 baseline，先跑 train'}")
            continue
        s = score(cur, base, spec["by"], direction=spec.get("direction", "both"),
                  min_abs=spec.get("min_abs", 0.0), opts=dopts, tz=tz)
        hits = s[s["anomaly"]]
        docs = []
        for r in hits.to_dict("records"):
            keys = {f: r[f] for f in spec["by"]}
            docs.append((doc_id(spec["name"], keys, r["ts"]), {
                "@timestamp": r["ts"].isoformat(), "series": spec["name"], "keys": keys, "value": r["value"],
                "median": r["median"], "expected_low": r["expected_low"], "expected_high": r["expected_high"],
                "z": r["z"], "level": r["level"], "direction": spec.get("direction", "both"), "detected_at": detected_at}))
        if docs:
            es.bulk_index(ANOMALY_INDEX, docs)
        total += len(docs)
        print(f"  {spec['name']}：{s.groupby(spec['by']).ngroups} 條序列、{len(s)} 個點，異常 {len(docs)} 個")
        for r in hits.sort_values("z", key=abs, ascending=False).head(5).to_dict("records"):
            keys = ", ".join(str(r[f]) for f in spec["by"])
            print(f"    {r['ts'].tz_convert(tz):%m-%d %H:%M}  {keys}：{r['value']:.3g}（平常 {r['median']:.3g}，"
                  f"範圍 {r['expected_low']:.3g}～{r['expected_high']:.3g}，z={r['z']:.1f}，{r['level']}）")
    print(f"共 {total} 個異常")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aiops_baseline")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--config", required=True)
    t.add_argument("--weeks", type=int)
    t.add_argument("--end")
    d = sub.add_parser("detect")
    d.add_argument("--config", required=True)
    d.add_argument("--window", default="30min")
    d.add_argument("--at")
    args = p.parse_args(argv)
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cmd_train(cfg, args) if args.cmd == "train" else cmd_detect(cfg, args)


if __name__ == "__main__":
    sys.exit(main())

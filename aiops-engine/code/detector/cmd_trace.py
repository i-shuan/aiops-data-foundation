"""trace 每一段的 baseline：trace-collect、trace-train、trace-detect（detector/trace.py、configs/baseline/trace.yaml）。"""
from __future__ import annotations

import pandas as pd

from .base import Anomaly
from .es import ANOMALY_INDEX, ANOMALY_MAPPING, BASELINE_INDEX, BASELINE_MAPPING, ES, doc_id, load_baseline
from . import adjust as A
from . import trace as T
from .model import DetectOptions, TrainOptions, score, train
from .cmd_baseline import save_anomalies, save_baseline, train_summary, with_status
from .common import RULE_FIELDS, _freq, _now
from .owner import write_anomalies
from .adjust import data_adjustments


def _metric_frame(df: pd.DataFrame, m: str) -> pd.DataFrame:
    return df[[*T.KEYS, "ts"]].assign(value=df[m])

def cmd_trace_merge(cfg: dict, args) -> int:
    """raw（每一段一筆）→ 一個請求一筆（traces-merged-<group>-<日期>）。排程器每分鐘跑；從上次合到的時間接著合，中斷很久會分段補（最多 2 天）。"""
    from . import trace_merge as M
    es = ES()
    at = _now(args.at).to_pydatetime()
    since, until, n = M.run(es, cfg, at, pd.Timedelta(args.window).to_pytimedelta())
    print(f"trace-merge {since:%H:%M:%S}～{until:%H:%M:%S}：合了 {n} 個 trace")
    return 0


def cmd_trace_collect(cfg: dict, args) -> int:
    """補過去的統計（trace 原文只留 7 天，最多補 7 天），或手動重算某一段。平常由 trace-detect 每 5 分鐘順便做。"""
    es = ES()
    freq = _freq(cfg.get("interval", "5m"))
    end = _now(args.end).floor(freq)
    start = pd.Timestamp(args.start) if args.start else end - pd.Timedelta(days=7)
    step = pd.Timedelta(hours=1)
    n = 0
    t = start
    while t < end:
        df, _ = T.collect(es, cfg, t, min(t + step, end))
        n += len(df)
        t += step
    print(f"統計 {start} ～ {end}：{n} 筆（API × 段 × 時段）寫進 {T.SEGMENT_INDEX}")
    return 0

def cmd_trace_train(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(BASELINE_INDEX, BASELINE_MAPPING)
    freq = _freq(cfg.get("interval", "5m"))
    end = _now(args.end).floor(freq)
    start = end - (pd.Timedelta(args.history) if args.history else pd.Timedelta(weeks=cfg.get("history_weeks", 4)))
    topts = TrainOptions(**cfg.get("train", {}))
    hist = T.load_segments(es, start, end)
    hist = hist[hist["requests"] >= cfg.get("min_requests", 5)]
    adjs = data_adjustments(es, cfg, end)
    print(f"學習區間 {start.tz_convert(topts.timezone):%Y-%m-%d %H:%M} ～ {end.tz_convert(topts.timezone):%Y-%m-%d %H:%M}"
          f"（{topts.timezone}），{len(hist)} 筆統計")
    for m in T.METRICS:
        h, dropped = A.train_filter(_metric_frame(hist, m), T.KEYS, f"trace.segment.{m}", adjs, topts.timezone)
        b = train(h, T.KEYS, topts)
        save_baseline(es, f"trace.segment.{m}", T.KEYS, b, start, end)
        print(train_summary(f"trace.segment.{m}", T.KEYS, h, b) + (f"；排除 {dropped} 個點" if dropped else ""))
    return 0

def cmd_trace_detect(cfg: dict, args) -> int:
    """算最近一段時間每個 API 每一段的統計（同時寫進 aiops-trace-segments，之後 train 用），跟 baseline 比。"""
    es = ES()
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    freq = _freq(cfg.get("interval", "5m"))
    end = _now(args.at).floor(freq)
    start = end - pd.Timedelta(args.window)
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    dopts = DetectOptions(**cfg.get("detect", {}))
    print(f"偵測區間 {start.tz_convert(tz):%Y-%m-%d %H:%M} ～ {end.tz_convert(tz):%Y-%m-%d %H:%M}（{tz}）")
    cur, skews = T.collect(es, cfg, start, end)
    adjs = data_adjustments(es, cfg, end)
    cur = cur[cur["requests"] >= cfg.get("min_requests", 5)]
    total = 0
    for m in T.METRICS:
        name = f"trace.segment.{m}"
        base = load_baseline(es, name, T.KEYS)
        if cur.empty or base.empty:
            print(f"  {name}：{'沒有資料' if cur.empty else '沒有 baseline，先跑 trace-train'}")
            continue
        # baseline 裡有、這次完全沒出現的段補 0（例如平常會查 DB 的那段整個消失）
        c = T.fill_missing_segments(cur, base[T.KEYS].drop_duplicates())
        rule = cfg.get("metrics", {}).get(m, {})
        s = score(_metric_frame(c, m), base, T.KEYS, direction=rule.get("direction", "both"),
                  min_abs=rule.get("min_abs", 0.0), opts=dopts, tz=tz)
        s, _ = A.detect_filter(s, T.KEYS, name, adjs, tz, baseline=base)
        s = with_status(cfg, s, T.KEYS, cfg.get("interval", "5m"), start)
        total += save_anomalies(es, name, T.KEYS, s, rule.get("direction", "both"), tz)
    # 沒見過的呼叫：規則，這支 API 有 baseline、但這一段從來沒出現過
    base = load_baseline(es, "trace.segment.calls", T.KEYS)
    new = T.new_segments(cur, base)
    now = pd.Timestamp.now(tz="UTC").isoformat()
    found = [Anomaly(ts=r["ts"], series="trace.segment.new", keys={k: r[k] for k in T.KEYS}, value=r["calls"],
                     median=0.0, expected_low=0.0, expected_high=0.0, level="rule", direction="up")
             for r in new.to_dict("records")]
    docs = [(a.id(), a.to_doc(now)) for a in found]
    if docs:
        write_anomalies(es, docs)
        for _, d in docs[:5]:
            print(f"  沒見過的呼叫：{d['keys']['entry.service']} {d['keys']['entry.name']} → {d['keys']['segment']}"
                  f"（每個請求 {d['value']:.1f} 次）")
    total += len(docs)
    # 不用學的規則：N+1（同一個呼叫太多次）、吞掉的例外
    rules = cfg.get("rules", {})
    found = []
    if rules.get("repeated_calls"):
        found += [("trace.segment.repeated", r, r["calls"], rules["repeated_calls"],
                   f"同一個呼叫太多次（可能是 N+1）：{r['entry.service']} {r['entry.name']} → {r['segment']}"
                   f"（每個請求 {r['calls']:.0f} 次，門檻 {rules['repeated_calls']}）")
                  for r in T.repeated_calls(cur, rules["repeated_calls"]).to_dict("records")]
    if rules.get("swallowed_rate"):
        found += [("trace.segment.swallowed", r, r["swallowed_rate"], rules["swallowed_rate"],
                   f"吞掉的例外：{r['entry.service']} {r['entry.name']} → {r['segment']}"
                   f"（{r['swallowed_rate']:.0%} 的請求丟了 {r.get('exception_type') or '例外'}，但沒標成錯誤）")
                  for r in T.swallowed(cur, rules["swallowed_rate"]).to_dict("records")]
    es.req("PUT", f"/{ANOMALY_INDEX}/_mapping", {"properties": RULE_FIELDS})
    docs = [(a.id(), a.to_doc(now)) for a in (
        Anomaly(ts=r["ts"], series=series, rule=series, keys={k: r[k] for k in T.KEYS}, value=v, expected_high=th,
                level="rule", direction="up", severity="warning", message=msg) for series, r, v, th, msg in found)]
    if docs:
        write_anomalies(es, docs)
        for _, d in docs[:10]:
            print(f"  [warning] {d['message']}")
    total += len(docs)
    # 時鐘不同步：規則，超過容許值就記
    # _id 不是預設的 (series, keys, ts)：既有資料就是這樣算的，換了會重複寫
    docs = [(doc_id("trace.clock_skew_ms", r["caller"], r["callee"], r["ts"]),
             Anomaly(ts=r["ts"], series="trace.clock_skew_ms", keys={"caller": r["caller"], "callee": r["callee"]}, value=r["skew_ms"],
                     median=0.0, expected_low=0.0, expected_high=cfg.get("clock_skew_ms", 500), level="rule", direction="up").to_doc())
            for r in skews]
    if docs:
        write_anomalies(es, docs)
        for _, d in docs[:5]:
            print(f"  時鐘不同步：{d['keys']['caller']} → {d['keys']['callee']} 差 {d['value']:.0f} ms")
    total += len(docs)
    print(f"共 {total} 個異常")
    return 0

"""用法（在 aiops 底下）：

  python3 -m aiops_baseline train  --config baseline/series.yaml [--weeks 4] [--end 2026-10-10T00:00:00Z]
  python3 -m aiops_baseline detect --config baseline/series.yaml [--window 30m] [--at 2026-10-10T08:00:00Z]
  python3 -m aiops_baseline rules  --config baseline/rules.yaml  [--at …] [--only 規則,…]
  python3 -m aiops_baseline peer   --config baseline/peer.yaml   [--at …] [--only 檢查,…]

train ：讀過去 N 週的資料，每條序列、每個時段學出中位數與波動範圍，寫進 ES 的 aiops-baseline。
detect：讀最近一段時間的資料，跟 baseline 比，超出範圍的寫進 aiops-anomalies，並印出來。
rules ：狀態變化、離極限多遠（不用學）。
peer  ：跟兄弟比，同一個服務的 Pod 互相比，抓只有一個 Pod 壞掉（不用學）。
--end、--at 用來回測（拿過去某個時間點當成「現在」）；不給就是現在。
"""
from __future__ import annotations

import argparse
import dataclasses
import sys

import pandas as pd
import yaml

from aiops_common.es import get_field

from .es import (ANOMALY_INDEX, ANOMALY_MAPPING, BASELINE_INDEX, BASELINE_MAPPING, ES, doc_id, fetch,
                 load_baseline, load_group_baselines, value_kind)
from . import peer as P
from . import rules as R
from . import trace as T
from .model import (CONFIDENCE, RESOLUTIONS, DetectOptions, TrainOptions, mask_thin, pick_resolution, score,
                    group_of, score_poisson, to_freq, train, train_groups)


def keys_of(spec: dict) -> list[str]:
    """序列依什麼切：有 variants（同一個指標、不同語言的欄位名稱）時用統一的 keys，否則就是 by。"""
    return spec.get("keys") or spec["by"]


_freq = to_freq


def _now(arg: str | None) -> pd.Timestamp:
    return pd.Timestamp(arg) if arg else pd.Timestamp.now(tz="UTC")


def _v(x):
    """DataFrame 合併後空的欄位是 NaN，ES 不收：轉成 None。"""
    return None if isinstance(x, float) and pd.isna(x) else x


def save_baseline(es, name: str, by: list[str], b: pd.DataFrame, start, end) -> None:
    """寫進 aiops-baseline，再刪掉這條序列上一次訓練留下、這次沒有的（例如改了粒度、或 4 週內都沒出現的 API）。"""
    trained_at = pd.Timestamp.now(tz="UTC").isoformat()
    docs = []
    for r in b.to_dict("records"):
        group = r.get("group") if isinstance(r.get("group"), str) else None
        keys = {"__group": group} if group else {f: r[f] for f in by}
        docs.append((doc_id(name, keys, r["dow"], r["hour"]), {
            "series": name, "keys": keys, "dow": r["dow"], "hour": r["hour"], "level": r["level"],
            "median": r["median"], "mad": r["mad"], "p05": r["p05"], "p95": r["p95"], "n": r["n"],
            "resolution": _v(r.get("resolution")), "method": _v(r.get("baseline_method")) or "mad",
            "baseline_source": f"group:{group}" if group else "self", "events": _v(r.get("events")),
            "trained_at": trained_at, "history_from": start.isoformat(), "history_to": end.isoformat()}))
    es.bulk_index(BASELINE_INDEX, docs)
    if docs:   # 這次什麼都沒學到（例如 ES 查不到資料）就不刪，保留舊的
        es.req("POST", f"/{BASELINE_INDEX}/_delete_by_query?conflicts=proceed", {"query": {"bool": {"filter": [
            {"term": {"series": name}}, {"range": {"trained_at": {"lt": trained_at}}}]}}})


def train_summary(name: str, by: list[str], hist: pd.DataFrame, b: pd.DataFrame) -> str:
    n_series = hist.groupby(by, dropna=False).ngroups if not hist.empty else 0
    n_points = int(hist["value"].notna().sum()) if not hist.empty else 0
    levels = b["level"].value_counts().to_dict() if not b.empty else {}
    res = ""
    if not b.empty and b.get("resolution") is not None and b["resolution"].notna().any():
        res = "，粒度 " + str(b.drop_duplicates(by, ignore_index=True)["resolution"].value_counts().to_dict())
    note = "（資料不夠，先不判斷）" if n_series and b.empty else ""
    return f"  {name}：{n_series} 條序列、{n_points} 個有效的點 → {len(b)} 個時段的 baseline {levels}{res}{note}"


def min_events(cfg: dict, spec: dict) -> float | None:
    """比例、百分位類在自己選粒度時，每個時間窗至少要有幾個事件才判斷；其他情況不限制（None）。"""
    ad = cfg.get("adaptive")
    if not ad or value_kind(spec) != "rate" or spec.get("resolution"):
        return None
    return ad.get("min_events", {}).get("rate", 20)


def series_resolutions(cfg: dict, spec: dict, hist: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """每條序列用什麼粒度：keys + resolution（None＝1 小時也不夠）+ events。

    設定檔沒有 adaptive、量測值（CPU、記憶體）、或序列自己寫死 resolution 時，全部用同一個粒度。
    """
    base = cfg.get("interval", "5m")
    ad = cfg.get("adaptive")
    kind = value_kind(spec)
    fixed = spec.get("resolution") or (None if ad and kind != "gauge" else base)
    if fixed:
        k = hist[keys].drop_duplicates() if not hist.empty else pd.DataFrame(columns=keys)
        return k.assign(resolution=fixed, events=None)
    return pick_resolution(hist[keys + ["ts", "n"]], keys, ad.get("min_events", {}).get(kind, 20 if kind == "rate" else 10),
                           base=base, resolutions=ad.get("resolutions", RESOLUTIONS), coverage=ad.get("coverage", 0.3))


def save_anomalies(es, name: str, by: list[str], s: pd.DataFrame, direction: str, tz: str,
                   resolution: str | None = None, note: str = "") -> int:
    """把 score 的結果裡異常的點寫進 aiops-anomalies，印出最嚴重的 5 個。回傳異常數。

    每筆帶 resolution（粒度）、method、baseline_source、confidence，讓根因分析知道這個 baseline 可不可信。
    """
    hits = s[s["anomaly"]]
    detected_at = pd.Timestamp.now(tz="UTC").isoformat()
    docs = []
    for r in hits.to_dict("records"):
        keys = {f: r[f] for f in by}
        source = _v(r.get("baseline_source")) or "self"
        level = r["level"] if source == "self" else "group"
        docs.append((doc_id(name, keys, r["ts"]), {
            "@timestamp": r["ts"].isoformat(), "series": name, "keys": keys, "value": r["value"],
            "median": r["median"], "expected_low": r["expected_low"], "expected_high": r["expected_high"],
            "z": r["z"], "level": level, "direction": direction, "detected_at": detected_at,
            "resolution": resolution, "method": _v(r.get("baseline_method")) or "mad", "baseline_source": source,
            "confidence": CONFIDENCE.get(level, "low"), **({"p": r["p"]} if "p" in r else {})}))
    if docs:
        es.bulk_index(ANOMALY_INDEX, docs)
    res = f"（{resolution} 一點{note}）" if resolution else ""
    print(f"  {name}{res}：{s.groupby(by, dropna=False).ngroups} 條序列、{len(s)} 個點，異常 {len(docs)} 個")
    for (_, d), r in sorted(zip(docs, hits.to_dict("records")), key=lambda x: -abs(x[1]["z"]))[:5]:
        keys = ", ".join(str(r[f]) for f in by)
        how = f"機率 {r['p']:.1e}，卜瓦松" if d["method"] == "poisson" else f"z={r['z']:.1f}"
        print(f"    {r['ts'].tz_convert(tz):%m-%d %H:%M}  {keys}：{r['value']:.3g}（平常 {r['median']:.3g}，"
              f"範圍 {r['expected_low']:.3g}～{r['expected_high']:.3g}，{how}，{d['level']}，可信度 {d['confidence']}）")
    return len(docs)


def cmd_train(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(BASELINE_INDEX, BASELINE_MAPPING)
    interval = cfg.get("interval", "5m")
    end = _now(args.end).floor(_freq(interval))
    start = end - pd.Timedelta(weeks=args.weeks or cfg.get("history_weeks", 4))
    topts = TrainOptions(**cfg.get("train", {}))
    print(f"學習區間 {start.tz_convert(topts.timezone):%Y-%m-%d %H:%M} ～ {end.tz_convert(topts.timezone):%Y-%m-%d %H:%M}"
          f"（{topts.timezone}，{interval} 一點）")
    for spec in cfg["series"]:
        keys = keys_of(spec)
        hist = fetch(es, spec, start, end, interval)
        res = series_resolutions(cfg, spec, hist, keys)
        parts = []
        for r, ks in res.dropna(subset=["resolution"]).groupby("resolution"):
            h = hist if r == interval else fetch(es, spec, start, end, r)   # 百分位不能從細的合併，粗的要重查
            h = h.merge(ks[keys], on=keys)
            if (me := min_events(cfg, spec)) is not None:
                h = mask_thin(h, me)
            t = train(h, keys, topts) if not h.empty else pd.DataFrame()
            if not t.empty:
                parts.append(t.merge(ks[keys + ["events"]], on=keys).assign(resolution=r))
        sparse = res[res["resolution"].isna()]
        n_poisson = 0
        # 很稀少的筆數（最粗的粒度也大多是 0）：改用卜瓦松，學每個時段的平均次數
        coarsest = (cfg.get("adaptive") or {}).get("resolutions", RESOLUTIONS)[-1]
        if not sparse.empty and value_kind(spec) == "count":
            h = fetch(es, spec, start, end, coarsest).merge(sparse[keys], on=keys)
            t = train(h, keys, topts, method="poisson") if not h.empty else pd.DataFrame()
            if not t.empty:
                parts.append(t.merge(sparse[keys + ["events"]], on=keys).assign(resolution=coarsest))
            sparse = sparse.merge(t[keys].drop_duplicates(), on=keys, how="left", indicator=True) if not t.empty else sparse
            if "_merge" in sparse:
                n_poisson = int((sparse["_merge"] == "both").sum())
                sparse = sparse[sparse["_merge"] == "left_only"].drop(columns="_merge")
        b = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        gb = pd.DataFrame()
        if spec.get("borrow") and cfg.get("groups"):
            # 從成員自己的 baseline 算出每組的 baseline，存在同一個 index，給沒有自己 baseline 的序列借
            gb = train_groups(b, keys, cfg["groups"], cfg.get("borrowing", {}).get("min_members", 3)).assign(
                resolution=borrow_resolution(cfg))
        save_baseline(es, spec["name"], keys, pd.concat([b, gb], ignore_index=True), start, end)
        print(train_summary(spec["name"], keys, hist, b))
        if n_poisson:
            print(f"    其中 {n_poisson} 條很稀少的筆數用卜瓦松（{coarsest} 一點）")
        if not gb.empty:
            print(f"    組 baseline（借給新的、量少的序列）：{gb['group'].unique().tolist()}")
        if not sparse.empty:
            print(f"    {len(sparse)} 條序列 1 小時也不夠（事件數夠的時間窗太少），沒有自己的 baseline："
                  + "、".join(", ".join(str(r[k]) for k in keys) for r in sparse.head(5).to_dict("records")))
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
    zeros = silent.merge(pd.DataFrame({"ts": grid}), how="cross").assign(value=0.0, n=0.0)
    return pd.concat([cur, zeros[by + ["ts", "value", "n"]]], ignore_index=True)


def borrow_resolution(cfg: dict) -> str:
    """借來的 baseline 用多粗的粒度判斷：預設最粗的（本來就是可信度低的判斷，用粗一點的窗比較穩）。"""
    return cfg.get("borrowing", {}).get("resolution") or (cfg.get("adaptive") or {}).get("resolutions", RESOLUTIONS)[-1]


def detect_borrowed(es, cfg: dict, spec: dict, own: pd.DataFrame, now, window, dopts, tz) -> int:
    """沒有自己 baseline 的序列（新 API、量太少）：借第一個符合、而且有組 baseline 的組來判斷，門檻調高。"""
    keys = keys_of(spec)
    gb = load_group_baselines(es, spec["name"])
    if gb.empty:
        return 0
    r = borrow_resolution(cfg)
    end_r = now.floor(_freq(r))
    start_r = end_r - max(window, pd.Timedelta(_freq(r)))
    cur = fetch(es, spec, start_r, end_r, r)
    if cur.empty:
        return 0
    if not own.empty:
        cur = cur.merge(own[keys].drop_duplicates(), on=keys, how="left", indicator=True)
        cur = cur[cur["_merge"] == "left_only"].drop(columns="_merge")
    if (me := min_events(cfg, spec)) is not None:
        cur = mask_thin(cur, me)
    have = set(gb["group"])
    pick = []
    for k in cur[keys].drop_duplicates().to_dict("records"):
        g = next((g for g in group_of(k, cfg["groups"]) if g in have), None)
        if g:
            pick.append({**k, "group": g})
    if not pick:
        return 0
    pick = pd.DataFrame(pick)
    base = pick.merge(gb.drop(columns="resolution"), on="group")
    bz = dataclasses.replace(dopts, z=cfg.get("borrowing", {}).get("z", 7.0))
    s = score(cur.merge(pick[keys], on=keys), base, keys, direction=spec.get("direction", "both"),
              min_abs=spec.get("min_abs", 0.0), opts=bz, tz=tz)
    s = s.merge(pick, on=keys, how="left")
    s["baseline_source"] = "group:" + s["group"].astype(str)
    return save_anomalies(es, spec["name"], keys, s.drop(columns="group"), spec.get("direction", "both"), tz, resolution=r,
                          note=f"，借 {'、'.join(sorted(pick['group'].unique()))} 的 baseline，z 門檻 {bz.z:g}")


def cmd_detect(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    interval = cfg.get("interval", "5m")
    now = _now(args.at)
    # 最後一段可能還沒收齊（筆數會偏低，看起來像「驟降」），所以只看已經結束的時段
    end = now.floor(_freq(interval))
    start = end - pd.Timedelta(args.window)
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    dopts = DetectOptions(**cfg.get("detect", {}))
    total = 0
    print(f"偵測區間 {start.tz_convert(tz):%Y-%m-%d %H:%M} ～ {end.tz_convert(tz):%Y-%m-%d %H:%M}（{tz}）")
    for spec in cfg["series"]:
        keys = keys_of(spec)
        base = load_baseline(es, spec["name"], keys)
        if spec.get("borrow") and cfg.get("groups"):
            total += detect_borrowed(es, cfg, spec, base, now, pd.Timedelta(args.window), dopts, tz)
        if base.empty:
            print(f"  {spec['name']}：沒有 baseline，先跑 train")
            continue
        base["resolution"] = base["resolution"].fillna(interval)
        # 每種粒度各查一次：1 小時一點的序列，看的是最近一個已經結束的整點（每 5 分鐘重跑會覆蓋同一筆，不會重複）
        for r, b in base.groupby("resolution"):
            end_r = now.floor(_freq(r))
            start_r = end_r - max(pd.Timedelta(args.window), pd.Timedelta(_freq(r)))
            cur = fetch(es, spec, start_r, end_r, r)
            ks = b[keys].drop_duplicates()
            cur = cur.merge(ks, on=keys) if not cur.empty else cur
            if value_kind(spec) == "count" or spec.get("fill_zero"):
                cur = _add_silent(cur, b, keys, start_r, end_r, r)
            if (me := min_events(cfg, spec)) is not None and not cur.empty:
                cur = mask_thin(cur, me)
            if cur.empty:
                print(f"  {spec['name']}（{r} 一點）：沒有資料")
                continue
            b = b.assign(baseline_method=b["baseline_method"].fillna("mad"))
            for method, bm in b.groupby("baseline_method"):
                fn = score_poisson if method == "poisson" else score
                cm = cur.merge(bm[keys].drop_duplicates(), on=keys)
                if cm.empty:
                    continue
                s = fn(cm, bm, keys, direction=spec.get("direction", "both"),
                       min_abs=spec.get("min_abs", 0.0), opts=dopts, tz=tz).assign(baseline_method=method)
                total += save_anomalies(es, spec["name"], keys, s, spec.get("direction", "both"), tz, resolution=r)
    print(f"共 {total} 個異常")
    return 0


# ── trace 每一段的 baseline（aiops_baseline/trace.py、baseline/trace.yaml）──

def _metric_frame(df: pd.DataFrame, m: str) -> pd.DataFrame:
    return df[[*T.KEYS, "ts"]].assign(value=df[m])


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
    print(f"學習區間 {start.tz_convert(topts.timezone):%Y-%m-%d %H:%M} ～ {end.tz_convert(topts.timezone):%Y-%m-%d %H:%M}"
          f"（{topts.timezone}），{len(hist)} 筆統計")
    for m in T.METRICS:
        h = _metric_frame(hist, m)
        b = train(h, T.KEYS, topts)
        save_baseline(es, f"trace.segment.{m}", T.KEYS, b, start, end)
        print(train_summary(f"trace.segment.{m}", T.KEYS, h, b))
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
        total += save_anomalies(es, name, T.KEYS, s, rule.get("direction", "both"), tz)
    # 沒見過的呼叫：規則，這支 API 有 baseline、但這一段從來沒出現過
    base = load_baseline(es, "trace.segment.calls", T.KEYS)
    new = T.new_segments(cur, base)
    now = pd.Timestamp.now(tz="UTC").isoformat()
    docs = [(doc_id("trace.segment.new", {k: r[k] for k in T.KEYS}, r["ts"]), {
        "@timestamp": r["ts"].isoformat(), "series": "trace.segment.new", "keys": {k: r[k] for k in T.KEYS},
        "value": r["calls"], "median": 0.0, "expected_low": 0.0, "expected_high": 0.0, "z": None, "level": "rule",
        "direction": "up", "detected_at": now}) for r in new.to_dict("records")]
    if docs:
        es.bulk_index(ANOMALY_INDEX, docs)
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
    docs = [(doc_id(series, {k: r[k] for k in T.KEYS}, r["ts"]), {
        "@timestamp": r["ts"].isoformat(), "series": series, "rule": series, "keys": {k: r[k] for k in T.KEYS},
        "value": v, "median": None, "expected_low": None, "expected_high": th, "z": None, "level": "rule",
        "direction": "up", "severity": "warning", "message": msg, "detected_at": now}) for series, r, v, th, msg in found]
    if docs:
        es.bulk_index(ANOMALY_INDEX, docs)
        for _, d in docs[:10]:
            print(f"  [warning] {d['message']}")
    total += len(docs)
    # 時鐘不同步：規則，超過容許值就記
    docs = [(doc_id("trace.clock_skew_ms", r["caller"], r["callee"], r["ts"]), {
        "@timestamp": r["ts"].isoformat(), "series": "trace.clock_skew_ms", "keys": {"caller": r["caller"], "callee": r["callee"]},
        "value": r["skew_ms"], "median": 0.0, "expected_low": 0.0, "expected_high": cfg.get("clock_skew_ms", 500),
        "z": None, "level": "rule", "direction": "up", "detected_at": pd.Timestamp.now(tz="UTC").isoformat()}) for r in skews]
    if docs:
        es.bulk_index(ANOMALY_INDEX, docs)
        for _, d in docs[:5]:
            print(f"  時鐘不同步：{d['keys']['caller']} → {d['keys']['callee']} 差 {d['value']:.0f} ms")
    total += len(docs)
    print(f"共 {total} 個異常")
    return 0


# ── 規則：狀態變化、離極限多遠（aiops_baseline/rules.py、baseline/rules.yaml）──

RULE_FIELDS = {"severity": {"type": "keyword"}, "message": {"type": "text"}, "rule": {"type": "keyword"}}


def cmd_rules(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    es.req("PUT", f"/{ANOMALY_INDEX}/_mapping", {"properties": RULE_FIELDS})   # 舊的 index 補上規則用的欄位
    now = _now(args.at)
    slot = now.floor(_freq(cfg.get("interval", "1m")))
    start = now - pd.Timedelta(_freq(cfg.get("lookback", "10m")))
    names = set(args.only.split(",")) if args.only else None
    docs, fired = [], 0
    for rule in cfg["rules"]:
        if names and rule["name"] not in names:
            continue
        hits = R.evaluate(rule, R.fetch(es, rule, start, now), now)
        for h in hits:
            keys = {f: v for f, v in zip(rule["by"], h["key"])}
            # 附帶的資訊（服務名稱、節點），根因分析用來對到服務；不參與「同一個對象」的判斷
            keys.update({f: v for f in rule.get("context", []) if (v := get_field(h["last"], f)) is not None})
            msg = R.describe(rule, h)
            docs.append((doc_id(f"rule.{rule['name']}", keys, slot), {
                "@timestamp": slot.isoformat(), "series": f"rule.{rule['name']}", "rule": rule["name"], "keys": keys,
                "value": h["value"], "median": None, "expected_low": None, "expected_high": rule["threshold"],
                "z": None, "level": "rule", "direction": rule.get("op", ">="), "severity": rule.get("severity", "warning"),
                "message": msg, "since": h["since"].isoformat(), "detected_at": pd.Timestamp.now(tz="UTC").isoformat()}))
            print(f"  [{rule.get('severity', 'warning')}] {msg}")
        fired += len(hits)
    if docs:
        es.bulk_index(ANOMALY_INDEX, docs)
    print(f"{slot.tz_convert('Asia/Taipei'):%m-%d %H:%M}：{len(cfg['rules'])} 條規則，觸發 {fired} 個")
    return 0


# ── 跟兄弟比：同一個服務的 Pod 互相比（aiops_baseline/peer.py、baseline/peer.yaml）──

SHOW = {"pct": lambda x: f"{x:.0%}", "seconds": lambda x: f"{x * 1000:.0f}ms", "cores": lambda x: f"{x:.2f} 核",
        "bytes": lambda x: f"{x / 1048576:.0f}MB"}


def cmd_peer(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    es.req("PUT", f"/{ANOMALY_INDEX}/_mapping", {"properties": RULE_FIELDS})
    end = _now(args.at)
    start = end - pd.Timedelta(_freq(cfg.get("window", "10m")))
    names = set(args.only.split(",")) if args.only else None
    docs, fired, checked = [], 0, 0
    for c in cfg["checks"]:
        if names and c["name"] not in names:
            continue
        checked += 1
        keys = keys_of(c)
        group, member = keys[:-1], keys[-1]
        cur = fetch(es, c, start, end, None)
        # 只比現在還在的 Pod：最近 fresh（預設 2 分鐘）內還有資料的。換版、縮容刪掉的 Pod 不算兄弟
        alive = fetch(es, P.count_spec(c), end - pd.Timedelta(_freq(cfg.get("fresh", "2m"))), end, None)
        cur = cur.merge(alive[keys], on=keys) if not cur.empty and not alive.empty else cur.iloc[0:0]
        if c.get("min_volume") and (vs := P.volume_spec(c)) is not None and not cur.empty:
            vol = fetch(es, vs, start, end, None).rename(columns={"value": "volume"})
            cur = cur.merge(vol[keys + ["volume"]], on=keys, how="left")
            cur = cur[cur["volume"].fillna(0) >= c["min_volume"]]
        res = P.compare(cur, group, member, direction=c.get("direction", "up"), z=cfg.get("z", 5.0),
                        min_abs=c.get("min_abs", 0.0), min_ratio=c.get("min_ratio"),
                        rel_floor=cfg.get("rel_floor", 0.1), min_peers=cfg.get("min_peers", 3))
        fmt = SHOW.get(c.get("show"), lambda x: f"{x:g}")
        n_groups = res.groupby(group).ngroups if not res.empty else 0
        for r in res[res["anomaly"]].to_dict("records"):
            k = {f: r[f] for f in keys}
            msg = P.describe(c, r, keys, fmt)
            docs.append((doc_id(c["name"], k, end.floor("1min")), {
                "@timestamp": end.floor("1min").isoformat(), "series": c["name"], "rule": c["name"], "keys": k,
                "value": r["value"], "median": r["median"], "expected_low": None, "expected_high": None, "z": r["z"],
                "level": "peer", "direction": c.get("direction", "up"), "severity": c.get("severity", "warning"),
                "message": msg, "detected_at": pd.Timestamp.now(tz="UTC").isoformat()}))
            print(f"  [{c.get('severity', 'warning')}] {msg}")
            fired += 1
        print(f"  {c['name']}：{n_groups} 組、{len(res)} 個 Pod 比過")
    if docs:
        es.bulk_index(ANOMALY_INDEX, docs)
    print(f"{end.tz_convert('Asia/Taipei'):%m-%d %H:%M}：{checked} 個檢查，觸發 {fired} 個")
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
    tc = sub.add_parser("trace-collect")
    tc.add_argument("--config", required=True)
    tc.add_argument("--start")
    tc.add_argument("--end")
    tt = sub.add_parser("trace-train")
    tt.add_argument("--config", required=True)
    tt.add_argument("--history", help="學多久的資料，例如 4w、2h（預設 history_weeks）")
    tt.add_argument("--end")
    td = sub.add_parser("trace-detect")
    td.add_argument("--config", required=True)
    td.add_argument("--window", default="30min")
    td.add_argument("--at")
    r = sub.add_parser("rules")
    r.add_argument("--config", required=True)
    r.add_argument("--at", help="回測：拿過去某個時間點當成現在")
    r.add_argument("--only", help="只跑這幾條（逗號分開）")
    pe = sub.add_parser("peer")
    pe.add_argument("--config", required=True)
    pe.add_argument("--at", help="回測：拿過去某個時間點當成現在")
    pe.add_argument("--only", help="只跑這幾個（逗號分開）")
    args = p.parse_args(argv)
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cmds = {"train": cmd_train, "detect": cmd_detect, "trace-collect": cmd_trace_collect,
            "trace-train": cmd_trace_train, "trace-detect": cmd_trace_detect, "rules": cmd_rules, "peer": cmd_peer}
    return cmds[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())

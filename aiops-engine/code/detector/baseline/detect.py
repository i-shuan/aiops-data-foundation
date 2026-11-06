"""detect：跟 baseline 比、超出範圍的寫進 aiops-anomalies；每輪寫一筆 detect-status（configs/baseline/series.yaml）。
score_params／status_params／with_status／apply_min_count 也給 tune --show、backtest、trace-detect 用。"""
from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from core.anomaly import Anomaly
from core.es_client import ES
from core.timeutil import _freq, _now, _v, keys_of
from sources import alive_times, as_backend, fetch, load_baseline, load_group_baselines, open_backend
from sources.es_aggs import value_kind
from sources.schema import ALERT_INDEX, ALERT_MAPPING, ANOMALY_INDEX, ANOMALY_MAPPING, stamp_of
from detector.adjust import adjust as A
from detector.adjust.tuning import data_adjustments, load_overrides
from detector.owner import write_anomalies

from .model import CONFIDENCE, DetectOptions, group_of, mark_status, mask_thin, override_for, score, score_poisson
from .train import borrow_resolution, min_events


def _keep_upgraded(es, docs: list) -> None:
    """不降級：同一個時間窗上一輪已經判成 anomaly（當時看得到前一個時間窗），這一輪查的範圍沒包含前一個時間窗、
    只能判成 warning 時，保留 anomaly。"""
    ids = [i for i, d in docs if d["status"] == "warning"]
    if not ids:
        return
    was = as_backend(es).anomaly_status(ids)
    for i, d in docs:
        if i in was:
            d["status"], d["streak"] = "anomaly", was[i].get("streak", d.get("streak"))

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
        status = _v(r.get("status")) or "anomaly"
        level = r["level"] if source == "self" else "group"
        a = Anomaly(ts=r["ts"], series=name, keys=keys, value=r["value"],
                    median=r["median"], expected_low=r["expected_low"], expected_high=r["expected_high"], z=r["z"],
                    level=level, direction=direction, detected_at=detected_at,
                    resolution=resolution, method=_v(r.get("baseline_method")) or "mad", baseline_source=source,
                    confidence=CONFIDENCE.get(level, "low"), status=status,
                    streak=int(r["streak"]) if _v(r.get("streak")) is not None else None,
                    p=r["p"] if "p" in r else None)
        docs.append((a.id(), a.to_doc()))    # 文件欄位只定義在 base.Anomaly；a.id() == doc_id(name, keys, ts)
    if docs:
        _keep_upgraded(es, docs)
        write_anomalies(es, docs)
    res = f"（{resolution} 一點{note}）" if resolution else ""
    n_warn = sum(d["status"] == "warning" for _, d in docs)
    print(f"  {name}{res}：{s.groupby(by, dropna=False).ngroups} 條序列、{len(s)} 個點，"
          f"異常 {len(docs) - n_warn} 個、warning {n_warn} 個")
    for (_, d), r in sorted(zip(docs, hits.to_dict("records")), key=lambda x: -abs(x[1]["z"]))[:5]:
        keys = ", ".join(str(r[f]) for f in by)
        how = f"機率 {r['p']:.1e}，卜瓦松" if d["method"] == "poisson" else f"z={r['z']:.1f}"
        print(f"    {r['ts'].tz_convert(tz):%m-%d %H:%M}  {keys}：{r['value']:.3g}（平常 {r['median']:.3g}，"
              f"範圍 {r['expected_low']:.3g}～{r['expected_high']:.3g}，{how}，{d['level']}，可信度 {d['confidence']}）"
              f"{'' if d['status'] == 'anomaly' else '  ← warning（跳一次，還沒連續）'}")
    return len(docs)

def _add_silent(cur: pd.DataFrame, base: pd.DataFrame, by: list[str], start, end, interval, alive=None) -> pd.DataFrame:
    """有 baseline、但這段時間完全沒資料的序列補 0：服務整個停掉、不再有請求或 log，也要抓得到。
    只補收集系統有在送資料的時間窗（alive：回傳時間窗的函式，見 es.alive_times）：收集系統停了的時段是「不知道」，
    補 0 的話每個服務都會被報成「停掉」。"""
    if base.empty:
        return cur
    known = base[by].drop_duplicates()
    seen = cur[by].drop_duplicates() if not cur.empty else known.iloc[0:0]
    silent = known.merge(seen, on=by, how="left", indicator=True)
    silent = silent[silent["_merge"] == "left_only"][by]
    if silent.empty:
        return cur
    grid = pd.date_range(start, end, freq=_freq(interval), inclusive="left")
    if alive is not None:
        grid = grid[grid.isin(alive())]
    if grid.empty:
        return cur
    zeros = silent.merge(pd.DataFrame({"ts": grid}), how="cross").assign(value=0.0, n=0.0)
    return pd.concat([cur, zeros[by + ["ts", "value", "n"]]], ignore_index=True)

def status_params(cfg: dict, spec: dict | None, key: dict, overrides=()) -> tuple[int, float | None]:
    """(confirm_points, instant_z)：aiops-tuning 的 override（依序列＋對象）＞ 這條序列的設定 ＞ series.yaml 的 status 全域值。"""
    st, spec = cfg.get("status", {}), spec or {}
    o = override_for(spec["name"], key, overrides) if spec.get("name") and overrides else {}
    cp = o.get("confirm_points", spec.get("confirm_points", st.get("confirm_points", 2)))
    iz = o.get("instant_z", spec.get("instant_z", st.get("instant_z")))
    return int(cp), (float(iz) if iz is not None else None)

def score_params(spec: dict, key: dict, dopts, overrides=()) -> tuple[float, float]:
    """(z, min_abs)：aiops-tuning 的 override（依序列＋對象）＞ 這條序列的 min_abs ＞ series.yaml 的 detect.z。score_tuned 和 tune --show 都用這個。"""
    o = override_for(spec["name"], key, overrides) if overrides else {}
    return float(o.get("z", dopts.z)), float(o.get("min_abs", spec.get("min_abs", 0.0)))

def with_status(cfg: dict, s: pd.DataFrame, keys: list[str], step: str, start, spec: dict | None = None, overrides=()) -> pd.DataFrame:
    """超標的點分 warning（跳一次）／anomaly（連續、或偏太多）。只回傳 start 之後的點：start 之前那一個時間窗是多查的，
    用來知道「前一個時間窗有沒有超標」，不重寫（它在上一輪已經判斷過）。
    「連續幾次才算」「偏多少不用等」依序列、依對象（status_params），同一組設定的對象一起算。"""
    within = cfg.get("status", {}).get("confirm_span", "30m")
    if s.empty:
        return mark_status(s, keys, step, 2, within)
    ks = s[keys].drop_duplicates()
    params = [status_params(cfg, spec, k, overrides) for k in ks.to_dict("records")]
    ks = ks.assign(_cp=[p[0] for p in params], _iz=[p[1] if p[1] is not None else np.nan for p in params])
    parts = []
    for (cp, iz), g in ks.groupby(["_cp", "_iz"], dropna=False):
        part = s.merge(g[keys], on=keys)
        parts.append(mark_status(part, keys, step, int(cp), within, None if pd.isna(iz) else float(iz)))
    out = pd.concat(parts, ignore_index=True)
    return out[out["ts"] >= start]

def apply_min_count(s: pd.DataFrame, spec: dict, keys: list[str] | None = None, overrides=()) -> pd.DataFrame:
    """比例類（錯誤率）：這個時間窗符合的事件數（值 × 底下的事件數，例如 5xx 個數）不到 min_count，不算異常。
    20 個請求錯 1 個就是 5%，已經等於 min_abs；一週合成資料這樣報了 205 次（docs/pdca/phase2-anomaly/2026-11-baseline-false-alarm.md）。
    aiops-tuning 的 override 可以依對象蓋 min_count（回饋「量太少」提的）：keys、overrides 有給時每列各自查。"""
    if s.empty or "n" not in s.columns:
        return s
    mc = pd.Series(spec.get("min_count"), index=s.index, dtype="float")
    if keys and overrides:
        for i, k in zip(s.index, s[keys].to_dict("records")):
            o = override_for(spec["name"], k, overrides).get("min_count")
            if o is not None:
                mc.at[i] = float(o)
    if mc.isna().all():
        return s
    hits = np.round(pd.to_numeric(s["value"], errors="coerce").fillna(0).to_numpy(dtype=float)
                    * pd.to_numeric(s["n"], errors="coerce").fillna(0).to_numpy(dtype=float))
    ok = mc.isna().to_numpy() | (hits >= mc.fillna(0).to_numpy())
    return s.assign(anomaly=s["anomaly"].to_numpy(dtype=bool) & ok)

def score_tuned(fn, cur: pd.DataFrame, base: pd.DataFrame, keys: list[str], spec: dict, dopts, tz, overrides) -> pd.DataFrame:
    """依每個對象的調整（z、min_abs）分組打分；沒有調整的用預設。"""
    if cur.empty:
        return cur.assign(anomaly=False)
    ks = cur[keys].drop_duplicates()
    params = [score_params(spec, k, dopts, overrides) for k in ks.to_dict("records")]
    ks = ks.assign(_z=[p[0] for p in params], _min_abs=[p[1] for p in params])
    parts = []
    for (z, min_abs), g in ks.groupby(["_z", "_min_abs"]):
        c = cur.merge(g[keys], on=keys)
        extra = {"buckets": spec["buckets"]} if fn is score and spec.get("buckets") else {}
        parts.append(fn(c, base, keys, direction=spec.get("direction", "both"), min_abs=min_abs,
                        opts=dataclasses.replace(dopts, z=z), tz=tz, **extra))
    return pd.concat(parts, ignore_index=True)

def detect_borrowed(es, cfg: dict, spec: dict, own: pd.DataFrame, now, window, dopts, tz, adjs=()) -> int:
    """沒有自己 baseline 的序列（新 API、量太少）：借第一個符合、而且有組 baseline 的組來判斷，門檻調高。"""
    keys = keys_of(spec)
    gb = load_group_baselines(es, spec["name"])
    if gb.empty:
        return 0
    r = borrow_resolution(cfg)
    end_r = now.floor(_freq(r))
    start_r = end_r - max(window, pd.Timedelta(_freq(r)))
    cur = fetch(es, spec, start_r - pd.Timedelta(_freq(r)), end_r, r)
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
              min_abs=spec.get("min_abs", 0.0), opts=bz, tz=tz, buckets=spec.get("buckets"))
    s = apply_min_count(s, spec)
    s, _ = A.detect_filter(s, keys, spec["name"], list(adjs), tz)
    s = with_status(cfg, s.merge(pick, on=keys, how="left"), keys, r, start_r, spec)
    s["baseline_source"] = "group:" + s["group"].astype(str)
    return save_anomalies(es, spec["name"], keys, s.drop(columns="group"), spec.get("direction", "both"), tz, resolution=r,
                          note=f"，借 {'、'.join(sorted(pick['group'].unique()))} 的 baseline，z 門檻 {bz.z:g}")

def detect_series(es, cfg: dict, spec: dict, base: pd.DataFrame, now, window: pd.Timedelta, dopts, tz, overrides, adjs,
                  quiet: bool = False) -> list[tuple[str, pd.DataFrame]]:
    """一條序列、每種粒度各算一次：回傳 [(粒度, 打完分、標好 status 的 DataFrame)]，不寫 ES。
    cmd_detect 拿去存；backtest（cmd_backtest.py）拿去比「現行設定 vs 建議的設定」。"""
    keys, interval = keys_of(spec), cfg.get("interval", "5m")
    # 剛結束的時間窗可能還沒收齊（Collector 批次送、ES refresh），往前退 lag 再看
    lag = pd.Timedelta(_freq(cfg.get("lag", "0s")))
    base = base.copy()
    base["resolution"] = base["resolution"].fillna(interval)
    out = []
    # 每種粒度各查一次：1 小時一點的序列，看的是最近一個已經結束的整點（每 5 分鐘重跑會覆蓋同一筆，不會重複）
    for r, b in base.groupby("resolution"):
        end_r = (now - lag).floor(_freq(r))
        start_r = end_r - max(window, pd.Timedelta(_freq(r)))
        # 多查前一個時間窗：判斷「連續」要知道前一個有沒有超標
        prev_r = start_r - pd.Timedelta(_freq(r))
        cur = fetch(es, spec, prev_r, end_r, r)
        ks = b[keys].drop_duplicates()
        cur = cur.merge(ks, on=keys) if not cur.empty else cur
        if value_kind(spec) == "count" or spec.get("fill_zero"):
            cur = _add_silent(cur, b, keys, prev_r, end_r, r,
                              alive=lambda: alive_times(es, spec, prev_r, end_r, r))
        if (me := min_events(cfg, spec)) is not None and not cur.empty:
            cur = mask_thin(cur, me)
        if cur.empty:
            if not quiet:
                print(f"  {spec['name']}（{r} 一點）：沒有資料")
            continue
        b = b.assign(baseline_method=b["baseline_method"].fillna("mad"))
        for method, bm in b.groupby("baseline_method"):
            fn = score_poisson if method == "poisson" else score
            cm = cur.merge(bm[keys].drop_duplicates(), on=keys)
            if cm.empty:
                continue
            s = apply_min_count(score_tuned(fn, cm, bm, keys, spec, dopts, tz, overrides).assign(baseline_method=method), spec, keys, overrides)
            s, muted = A.detect_filter(s, keys, spec["name"], adjs, tz, baseline=bm)
            if muted and not quiet:
                print(f"  {spec['name']}（{r} 一點）：{muted} 個點在排除時段、週期放寬、重置點之後，不算")
            out.append((r, with_status(cfg, s, keys, r, start_r, spec, overrides)))
    return out


def write_detect_status(es, now: pd.Timestamp, missing: list[str]) -> None:
    """每輪寫一筆 _id detect-status 進 aiops-alerts，跟 investigator alert 的心跳同一個形狀（status ok，不是一件事；
    事件整合、儀表板、metrics 都只拿 status open／category service 的，不會把它當成一件）。
    missing_baseline 是這輪沒有 baseline 的序列數。aiops-baseline 掉了（ES 重建、index 被刪）時 detect 只印一行就跳過，
    動態基準線這一路靜默停擺，aiops-heartbeat 看不到；Grafana 的 aiops-baseline-missing 規則查 missing_baseline > 0 送平台
    （grafana/helmchart/files/alert-rules.yaml、docs/pdca/meta/2026-10-open-issues.md 第 7 節）。"""
    es.ensure_index(ALERT_INDEX, ALERT_MAPPING)    # 既有 index 補 missing_baseline 欄位（dynamic false；ensure_index 會 PUT _mapping）
    es.bulk_index(ALERT_INDEX, [("detect-status", {
        "@timestamp": stamp_of(now).isoformat(), "alert_id": "detect-status", "category": "status", "status": "ok",
        "service": "aiops", "group": "unassigned", "title": "detector detect 狀態",
        "started_at": now.isoformat(), "updated_at": now.isoformat(),
        "anomaly_ids": [], "series": list(missing), "routes": [], "anomaly_count": 0, "missing_baseline": len(missing)})])


def cmd_detect(cfg: dict, args, backend=None) -> int:
    es = backend or open_backend(cfg, ES)
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    interval = cfg.get("interval", "5m")
    now = _now(args.at)
    # 最後一段可能還沒收齊（筆數會偏低，看起來像「驟降」），所以只看已經結束的時段
    end = now.floor(_freq(interval))
    start = end - pd.Timedelta(args.window)
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    dopts = DetectOptions(**cfg.get("detect", {}))
    overrides = load_overrides(es, cfg, now)   # --at 回放時以 --at 判斷有沒有過期（跟排除時段、重置點、backtest 一樣）
    adjs = data_adjustments(es, cfg, now)
    total, missing = 0, []
    print(f"偵測區間 {start.tz_convert(tz):%Y-%m-%d %H:%M} ～ {end.tz_convert(tz):%Y-%m-%d %H:%M}（{tz}）")
    for spec in cfg["series"]:
        keys = keys_of(spec)
        base = load_baseline(es, spec["name"], keys)
        if spec.get("borrow") and cfg.get("groups"):
            total += detect_borrowed(es, cfg, spec, base, now, pd.Timedelta(args.window), dopts, tz, adjs)
        if base.empty:
            print(f"  {spec['name']}：沒有 baseline，先跑 train")
            missing.append(spec["name"])
            continue
        for r, s in detect_series(es, cfg, spec, base, now, pd.Timedelta(args.window), dopts, tz, overrides, adjs):
            total += save_anomalies(es, spec["name"], keys, s, spec.get("direction", "both"), tz, resolution=r)
    write_detect_status(es, now, missing)
    print(f"共 {total} 個異常" + (f"；{len(missing)} 條序列沒有 baseline（已寫 detect-status）" if missing else ""))
    return 0

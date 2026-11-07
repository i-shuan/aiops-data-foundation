"""train、detect、tune：學「平常的樣子」、跟 baseline 比、使用者調粒度與門檻（configs/baseline/series.yaml）。"""
from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from common.infra.es import ES, doc_id
from common.infra.clock import stamp_of
from detect.config.types import Status
from detect.config.series import Series, SeriesConfig
from detect.config.trace import TraceConfig
from detect.store.models import Anomaly
from common.infra.repos import Repos
from common.scenario import load_scenario
import detect.series_ops.manual_adjust as A
from detect.strategy.dynamic_baseline import (CONFIDENCE, RESOLUTIONS, DetectOptions, TrainOptions, effective_for, mask_thin, pick_resolution, score, group_of, mark_status, override_for, resolutions_from, score_poisson, train, train_groups)
from detect.utils import _freq, _now, _v, keys_of, value_kind
from detect.series_ops.manual_adjust import data_adjustments, load_overrides


def save_baseline(repo: Repos, name: str, by: list[str], b: pd.DataFrame, start, end, had_data: bool = False) -> None:
    """寫進 aiops-baseline，再刪掉這條序列上一次訓練留下、這次沒有的（例如改了粒度、或 4 週內都沒出現的 API）。
    had_data：這次有查到資料（只是不夠學）。ES 查不到任何資料時不刪，保留舊的。"""
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
    repo.baseline.save(name, docs, trained_at, had_data)

def train_summary(name: str, by: list[str], hist: pd.DataFrame, b: pd.DataFrame) -> str:
    n_series = hist.groupby(by, dropna=False).ngroups if not hist.empty else 0
    n_points = int(hist["value"].notna().sum()) if not hist.empty else 0
    levels = b["level"].value_counts().to_dict() if not b.empty else {}
    res = ""
    if not b.empty and b.get("resolution") is not None and b["resolution"].notna().any():
        res = "，粒度 " + str(b.drop_duplicates(by, ignore_index=True)["resolution"].value_counts().to_dict())
    note = "（資料不夠，先不判斷）" if n_series and b.empty else ""
    return f"  {name}：{n_series} 條序列、{n_points} 個有效的點 → {len(b)} 個時段的 baseline {levels}{res}{note}"

def min_events(series_config: SeriesConfig, spec: Series) -> float | None:
    """比例、百分位類在自己選粒度時，每個時間窗至少要有幾個事件才判斷；其他情況不限制（None）。"""
    ad = series_config.adaptive
    if not ad or value_kind(spec) != "rate" or spec.get("resolution"):
        return None
    return ad.min_events.rate

def apply_resolution_overrides(res: pd.DataFrame, spec: dict, keys: list[str], overrides: list[dict]) -> pd.DataFrame:
    """使用者設定、tune、feedback 指定的粒度，蓋過自動選的。"""
    if res.empty or not overrides:
        return res
    res = res.copy()
    for i, k in zip(res.index, res[keys].to_dict("records")):
        if (r := override_for(spec["name"], k, overrides).get("resolution")):
            res.at[i, "resolution"] = r
    return res

def series_resolutions(series_config: SeriesConfig, spec: Series, hist: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """每條序列用什麼粒度：keys + resolution（None＝1 小時也不夠）+ events。

    設定檔沒有 adaptive、量測值（CPU、記憶體）、或序列自己寫死 resolution 時，全部用同一個粒度。
    """
    base = series_config.interval
    ad = series_config.adaptive
    kind = value_kind(spec)
    fixed = spec.get("resolution") or (None if ad and kind != "gauge" else base)
    if fixed:
        k = hist[keys].drop_duplicates() if not hist.empty else pd.DataFrame(columns=keys)
        return k.assign(resolution=fixed, events=None)
    res = pick_resolution(hist[keys + ["ts", "n"]], keys, getattr(ad.min_events, kind),
                          base=base, resolutions=resolutions_from(ad.resolutions or RESOLUTIONS, spec.get("min_resolution")),
                          coverage=ad.coverage)
    return res

def _keep_upgraded(repo: Repos, docs: list) -> None:
    """不降級：同一個時間窗上一輪已經判成 anomaly（當時看得到前一個時間窗），這一輪查的範圍沒包含前一個時間窗、
    只能判成 warning 時，保留 anomaly。"""
    ids = [i for i, d in docs if d["status"] == "warning"]
    if not ids:
        return
    was = repo.anomalies.status_of(ids)
    for i, d in docs:
        if i in was:
            d["status"], d["streak"] = "anomaly", was[i].get("streak", d.get("streak"))

def save_anomalies(repo: Repos, name: str, by: list[str], s: pd.DataFrame, direction: str, tz: str,
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
        _keep_upgraded(repo, docs)
        repo.anomalies.write(docs)
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

def cmd_train(series_config: SeriesConfig, args, repo: Repos | None = None) -> int:
    repo = repo or Repos(ES(), load_scenario())   # 測試給記憶體版的 repo（tests/fakes.py）
    repo.baseline.ensure()
    interval = series_config.interval
    end = _now(args.end).floor(_freq(interval))
    start = end - pd.Timedelta(weeks=args.weeks or series_config.history_weeks)
    # 5 分鐘以下的細粒度只學最近 fine_history（30 秒一點 4 週是 8 萬點；14 天還夠「同星期幾 × 同小時」用）
    fh = series_config.fine_history
    fine_start = max(start, end - pd.Timedelta(fh[:-1] + "D" if fh.endswith("d") else fh))
    fine = lambda r: pd.Timedelta(_freq(r)) < pd.Timedelta("5min")
    topts = TrainOptions(**series_config.train.to_dict())
    overrides = load_overrides(repo, series_config, end)    # 有沒有過期以這次 train 的時間點判斷（跟排除時段、重置點一樣）
    adjs = data_adjustments(repo, series_config, end)
    print(f"學習區間 {start.tz_convert(topts.timezone):%Y-%m-%d %H:%M} ～ {end.tz_convert(topts.timezone):%Y-%m-%d %H:%M}"
          f"（{topts.timezone}，{interval} 一點；5 分鐘以下的粒度學最近 {series_config.fine_history}）")
    if adjs:
        print(f"  {len(adjs)} 筆排除時段、重置點、週期放寬生效中")
    for spec in series_config.series:
        keys = keys_of(spec)
        # 排除時段、重置點之前、要排除的週期時段：這些點不學（adjust.py）
        hist_start = fine_start if fine(interval) else start
        hist = repo.series.fetch(spec, hist_start, end, interval)
        had_data = not hist.empty
        hist, dropped = A.train_filter(hist, keys, spec["name"], adjs, topts.timezone)
        excluded = lambda df: A.train_filter(df, keys, spec["name"], adjs, topts.timezone)[0]
        res = apply_resolution_overrides(series_resolutions(series_config, spec, hist, keys), spec, keys, overrides)
        parts = []
        for r, ks in res.dropna(subset=["resolution"]).groupby("resolution"):
            r_start = fine_start if fine(r) else start
            # 百分位不能從細的合併，粗的要重查
            h = hist if (r == interval and r_start == hist_start) else excluded(repo.series.fetch(spec, r_start, end, r))
            h = h.merge(ks[keys], on=keys)
            if (me := min_events(series_config, spec)) is not None:
                h = mask_thin(h, me)
            t = train(h, keys, topts) if not h.empty else pd.DataFrame()
            if not t.empty:
                parts.append(t.merge(ks[keys + ["events"]], on=keys).assign(resolution=r))
        sparse = res[res["resolution"].isna()]
        n_poisson = 0
        # 很稀少的筆數（最粗的粒度也大多是 0）：改用卜瓦松，學每個時段的平均次數
        coarsest = (series_config.adaptive.resolutions if series_config.adaptive and series_config.adaptive.resolutions else RESOLUTIONS)[-1]
        if not sparse.empty and value_kind(spec) == "count":
            h = excluded(repo.series.fetch(spec, start, end, coarsest)).merge(sparse[keys], on=keys)
            t = train(h, keys, topts, method="poisson") if not h.empty else pd.DataFrame()
            if not t.empty:
                parts.append(t.merge(sparse[keys + ["events"]], on=keys).assign(resolution=coarsest))
            sparse = sparse.merge(t[keys].drop_duplicates(), on=keys, how="left", indicator=True) if not t.empty else sparse
            if "_merge" in sparse:
                n_poisson = int((sparse["_merge"] == "both").sum())
                sparse = sparse[sparse["_merge"] == "left_only"].drop(columns="_merge")
        b = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        gb = pd.DataFrame()
        if spec.get("borrow") and series_config.groups:
            # 從成員自己的 baseline 算出每組的 baseline，存在同一個 index，給沒有自己 baseline 的序列借
            gb = train_groups(b, keys, series_config.groups, series_config.borrowing.min_members).assign(
                resolution=borrow_resolution(series_config))
        save_baseline(repo, spec["name"], keys, pd.concat([b, gb], ignore_index=True), start, end, had_data=had_data)
        print(train_summary(spec["name"], keys, hist, b))
        if dropped:
            print(f"    排除 {dropped} 個點（排除時段、重置點之前、要排除的週期時段）")
        if n_poisson:
            print(f"    其中 {n_poisson} 條很稀少的筆數用卜瓦松（{coarsest} 一點）")
        if not gb.empty:
            print(f"    組 baseline（借給新的、量少的序列）：{gb['group'].unique().tolist()}")
        if not sparse.empty:
            print(f"    {len(sparse)} 條序列 1 小時也不夠（事件數夠的時間窗太少），沒有自己的 baseline："
                  + "、".join(", ".join(str(r[k]) for k in keys) for r in sparse.head(5).to_dict("records")))
    return 0

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

def status_params(config: SeriesConfig | TraceConfig, spec: Series | None, key: dict, overrides=()) -> tuple[int, float | None]:
    """(confirm_points, instant_z)：aiops-tuning 的 override（依序列＋對象）＞ 這條序列的設定 ＞ series.yaml 的 status 全域值。"""
    st, spec = config.status or Status(), spec or {}     # Status() 是「全部沒寫」，st.get(鍵, 預設) 照舊回預設
    o = override_for(spec["name"], key, overrides) if spec.get("name") and overrides else {}
    cp = o.get("confirm_points", spec.get("confirm_points", st.get("confirm_points", 2)))
    iz = o.get("instant_z", spec.get("instant_z", st.get("instant_z")))
    return int(cp), (float(iz) if iz is not None else None)

def score_params(spec: dict, key: dict, dopts, overrides=()) -> tuple[float, float]:
    """(z, min_abs)：aiops-tuning 的 override（依序列＋對象）＞ 這條序列的 min_abs ＞ series.yaml 的 detect.z。score_tuned 和 tune --show 都用這個。"""
    o = override_for(spec["name"], key, overrides) if overrides else {}
    return float(o.get("z", dopts.z)), float(o.get("min_abs", spec.get("min_abs", 0.0)))

def effective_lines(series_config: SeriesConfig, spec: Series, key: dict, overrides) -> list[str]:
    """tune --show 印的每一行：這條序列、這個對象最終生效的粒度、z、min_abs、confirm_points、instant_z，各自來自哪一筆。
    值一律從 detect 實際用的函式拿（score_params、status_params），來源從 effective_for 拿，兩邊不會各寫一套。"""
    eff = effective_for(spec["name"], key, overrides)
    z, min_abs = score_params(spec, key, DetectOptions(**series_config.detect.to_dict()), overrides)
    cp, iz = status_params(series_config, spec, key, overrides)
    # 粒度在 train 時套（apply_resolution_overrides），這裡只能說「有 override 就是它，沒有就是序列寫死的或自動選的」
    res = eff["resolution"]["value"] if "resolution" in eff else spec.get("resolution") or "自動（train 依事件數選，看 aiops-baseline 的 resolution）"
    values = {"resolution": res, "z": z, "min_abs": min_abs, "confirm_points": cp, "instant_z": iz}
    lines = []
    for k, v in values.items():
        if k in eff:
            e = eff[k]
            who = ", ".join(f"{f}={p}" for f, p in (e.get("match") or {}).items()) or "全部"
            src = f"來自 {e['source']}：{e.get('series') or '.*'} {who}"
        else:
            src = "series.yaml 預設"
        lines.append(f"  {k} = {'無' if v is None else v}（{src}）")
    return lines


def with_status(config: SeriesConfig | TraceConfig, s: pd.DataFrame, keys: list[str], step: str, start, spec: dict | None = None, overrides=()) -> pd.DataFrame:
    """超標的點分 warning（跳一次）／anomaly（連續、或偏太多）。只回傳 start 之後的點：start 之前那一個時間窗是多查的，
    用來知道「前一個時間窗有沒有超標」，不重寫（它在上一輪已經判斷過）。
    「連續幾次才算」「偏多少不用等」依序列、依對象（status_params），同一組設定的對象一起算。"""
    span = (config.status or Status()).get("confirm_span", "30m")
    if s.empty:
        return mark_status(s, keys, step, 2, span)
    ks = s[keys].drop_duplicates()
    params = [status_params(config, spec, k, overrides) for k in ks.to_dict("records")]
    ks = ks.assign(_cp=[p[0] for p in params], _iz=[p[1] if p[1] is not None else np.nan for p in params])
    parts = []
    for (cp, iz), g in ks.groupby(["_cp", "_iz"], dropna=False):
        part = s.merge(g[keys], on=keys)
        parts.append(mark_status(part, keys, step, int(cp), span, None if pd.isna(iz) else float(iz)))
    out = pd.concat(parts, ignore_index=True)
    return out[out["ts"] >= start]

def cmd_tune(series_config: SeriesConfig, args) -> int:
    """寫一筆調整進 aiops-tuning（同一條序列＋同一組 match 會覆蓋）；--remove 刪掉；--list 列出；
    --show 看某條序列、某個對象最終生效的值與來源（多筆都符合時後面蓋前面，不然看不出是哪一筆定的）。"""
    repo = Repos(ES(), load_scenario())
    repo.tuning.ensure()
    if args.list:
        for o in load_overrides(repo, series_config):
            print(A.describe(o))
        return 0
    match = dict(m.split("=", 1) for m in (args.match or []))
    if args.show:
        if not args.series or not match:
            print("--show 要給 --series（series.yaml 的名稱）和 --match（對象的實際值，例如 service=mes-api）")
            return 2
        spec = next((s for s in series_config.series if s["name"] == args.series), None)
        if spec is None:
            print(f"series.yaml 沒有這條序列：{args.series}")
            return 2
        print(f"{args.series} {match} 現在生效的值：")
        for line in effective_lines(series_config, spec, match, load_overrides(repo, series_config)):
            print(line)
        return 0
    _id = doc_id("tune", args.series, match)
    if args.remove:
        repo.tuning.delete(_id)
        print(f"已刪除：{args.series} {match}")
        return 0
    doc = {"kind": "override", "series": args.series, "match": match, "resolution": args.resolution, "z": args.z, "min_abs": args.min_abs,
           "confirm_points": args.confirm_points, "instant_z": args.instant_z,
           "reason": args.reason, "by": args.by, "at": pd.Timestamp.now(tz="UTC").isoformat()}
    repo.tuning.put(_id, {k: v for k, v in doc.items() if v is not None})
    print(f"已調整：{args.series} {match} → " + "，".join(f"{k}={doc[k]}" for k in ("resolution", "z", "min_abs", "confirm_points", "instant_z") if doc[k] is not None)
          + "（粒度下次 train 生效；z、min_abs、confirm_points、instant_z 下次 detect 生效）")
    return 0

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

def borrow_resolution(series_config: SeriesConfig) -> str:
    """借來的 baseline 用多粗的粒度判斷：預設最粗的（本來就是可信度低的判斷，用粗一點的窗比較穩）。"""
    return series_config.borrowing.resolution or (series_config.adaptive.resolutions if series_config.adaptive and series_config.adaptive.resolutions else RESOLUTIONS)[-1]

def detect_borrowed(repo: Repos, series_config: SeriesConfig, spec: Series, own: pd.DataFrame, now, window, dopts, tz, adjs=()) -> int:
    """沒有自己 baseline 的序列（新 API、量太少）：借第一個符合、而且有組 baseline 的組來判斷，門檻調高。"""
    keys = keys_of(spec)
    gb = repo.baseline.load_group(spec["name"])
    if gb.empty:
        return 0
    r = borrow_resolution(series_config)
    end_r = now.floor(_freq(r))
    start_r = end_r - max(window, pd.Timedelta(_freq(r)))
    cur = repo.series.fetch(spec, start_r - pd.Timedelta(_freq(r)), end_r, r)
    if cur.empty:
        return 0
    if not own.empty:
        cur = cur.merge(own[keys].drop_duplicates(), on=keys, how="left", indicator=True)
        cur = cur[cur["_merge"] == "left_only"].drop(columns="_merge")
    if (me := min_events(series_config, spec)) is not None:
        cur = mask_thin(cur, me)
    have = set(gb["group"])
    pick = []
    for k in cur[keys].drop_duplicates().to_dict("records"):
        g = next((g for g in group_of(k, series_config.groups) if g in have), None)
        if g:
            pick.append({**k, "group": g})
    if not pick:
        return 0
    pick = pd.DataFrame(pick)
    base = pick.merge(gb.drop(columns="resolution"), on="group")
    bz = dataclasses.replace(dopts, z=series_config.borrowing.z)
    s = score(cur.merge(pick[keys], on=keys), base, keys, direction=spec.get("direction", "both"),
              min_abs=spec.get("min_abs", 0.0), opts=bz, tz=tz, buckets=spec.get("buckets"))
    s = apply_min_count(s, spec)
    s, _ = A.detect_filter(s, keys, spec["name"], list(adjs), tz)
    s = with_status(series_config, s.merge(pick, on=keys, how="left"), keys, r, start_r, spec)
    s["baseline_source"] = "group:" + s["group"].astype(str)
    return save_anomalies(repo, spec["name"], keys, s.drop(columns="group"), spec.get("direction", "both"), tz, resolution=r,
                          note=f"，借 {'、'.join(sorted(pick['group'].unique()))} 的 baseline，z 門檻 {bz.z:g}")

def detect_series(repo: Repos, series_config: SeriesConfig, spec: Series, base: pd.DataFrame, now, window: pd.Timedelta, dopts, tz, overrides, adjs,
                  quiet: bool = False) -> list[tuple[str, pd.DataFrame]]:
    """一條序列、每種粒度各算一次：回傳 [(粒度, 打完分、標好 status 的 DataFrame)]，不寫 ES。
    cmd_detect 拿去存；backtest（cmd_backtest.py）拿去比「現行設定 vs 建議的設定」。"""
    keys, interval = keys_of(spec), series_config.interval
    # 剛結束的時間窗可能還沒收齊（Collector 批次送、ES refresh），往前退 lag 再看
    lag = pd.Timedelta(_freq(series_config.lag))
    base = base.copy()
    base["resolution"] = base["resolution"].fillna(interval)
    out = []
    # 每種粒度各查一次：1 小時一點的序列，看的是最近一個已經結束的整點（每 5 分鐘重跑會覆蓋同一筆，不會重複）
    for r, b in base.groupby("resolution"):
        end_r = (now - lag).floor(_freq(r))
        start_r = end_r - max(window, pd.Timedelta(_freq(r)))
        # 多查前一個時間窗：判斷「連續」要知道前一個有沒有超標
        prev_r = start_r - pd.Timedelta(_freq(r))
        cur = repo.series.fetch(spec, prev_r, end_r, r)
        ks = b[keys].drop_duplicates()
        cur = cur.merge(ks, on=keys) if not cur.empty else cur
        if value_kind(spec) == "count" or spec.get("fill_zero"):
            cur = _add_silent(cur, b, keys, prev_r, end_r, r,
                              alive=lambda: repo.series.alive_times(spec, prev_r, end_r, r))
        if (me := min_events(series_config, spec)) is not None and not cur.empty:
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
            out.append((r, with_status(series_config, s, keys, r, start_r, spec, overrides)))
    return out


def write_detect_status(repo: Repos, now: pd.Timestamp, missing: list[str]) -> None:
    """每輪寫一筆 _id detect-status 進 aiops-alerts，跟 investigator alert 的心跳同一個形狀（status ok，不是一件事；
    事件整合、儀表板、metrics 都只拿 status open／category service 的，不會把它當成一件）。
    missing_baseline 是這輪沒有 baseline 的序列數。aiops-baseline 掉了（ES 重建、index 被刪）時 detect 只印一行就跳過，
    動態基準線這一路靜默停擺，aiops-heartbeat 看不到；Grafana 的 aiops-baseline-missing 規則查 missing_baseline > 0 送平台
    （grafana/helmchart/files/alert-rules.yaml、docs/pdca/meta/2026-10-open-issues.md 第 7 節）。"""
    repo.alerts.ensure()    # 既有 index 補 missing_baseline 欄位（dynamic false；ensure 會 PUT _mapping）
    repo.alerts.bulk([("detect-status", {
        "@timestamp": stamp_of(now).isoformat(), "alert_id": "detect-status", "category": "status", "status": "ok",
        "service": "aiops", "group": "unassigned", "title": "detector detect 狀態",
        "started_at": now.isoformat(), "updated_at": now.isoformat(),
        "anomaly_ids": [], "series": list(missing), "routes": [], "anomaly_count": 0, "missing_baseline": len(missing)})])


def cmd_detect(series_config: SeriesConfig, args, repo: Repos | None = None) -> int:
    repo = repo or Repos(ES(), load_scenario())
    repo.anomalies.ensure()
    interval = series_config.interval
    now = _now(args.at)
    # 最後一段可能還沒收齊（筆數會偏低，看起來像「驟降」），所以只看已經結束的時段
    end = now.floor(_freq(interval))
    start = end - pd.Timedelta(args.window)
    tz = series_config.train.timezone or "Asia/Taipei"
    dopts = DetectOptions(**series_config.detect.to_dict())
    overrides = load_overrides(repo, series_config, now)   # --at 回放時以 --at 判斷有沒有過期（跟排除時段、重置點、backtest 一樣）
    adjs = data_adjustments(repo, series_config, now)
    total, missing = 0, []
    print(f"偵測區間 {start.tz_convert(tz):%Y-%m-%d %H:%M} ～ {end.tz_convert(tz):%Y-%m-%d %H:%M}（{tz}）")
    for spec in series_config.series:
        keys = keys_of(spec)
        base = repo.baseline.load(spec["name"], keys)
        if spec.get("borrow") and series_config.groups:
            total += detect_borrowed(repo, series_config, spec, base, now, pd.Timedelta(args.window), dopts, tz, adjs)
        if base.empty:
            print(f"  {spec['name']}：沒有 baseline，先跑 train")
            missing.append(spec["name"])
            continue
        for r, s in detect_series(repo, series_config, spec, base, now, pd.Timedelta(args.window), dopts, tz, overrides, adjs):
            total += save_anomalies(repo, spec["name"], keys, s, spec.get("direction", "both"), tz, resolution=r)
    write_detect_status(repo, now, missing)
    print(f"共 {total} 個異常" + (f"；{len(missing)} 條序列沒有 baseline（已寫 detect-status）" if missing else ""))
    return 0

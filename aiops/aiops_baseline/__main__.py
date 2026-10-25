"""用法（在 aiops 底下）：

  python3 -m aiops_baseline train  --config baseline/series.yaml [--weeks 4] [--end 2026-10-10T00:00:00Z]
  python3 -m aiops_baseline detect --config baseline/series.yaml [--window 30m] [--at 2026-10-10T08:00:00Z]
  python3 -m aiops_baseline tune   --series http.latency_p95_s --match service=mes-api --resolution 5m --reason 誤報多
  python3 -m aiops_baseline adjust add --kind exclude_window --match service=mes-api --start … --end … --reason 事故
  python3 -m aiops_baseline adjust list | approve --id … | remove --id …
  python3 -m aiops_baseline rules  --config baseline/rules.yaml  [--at …] [--only 規則,…]
  python3 -m aiops_baseline peer   --config baseline/peer.yaml   [--at …] [--only 檢查,…]
  python3 -m aiops_baseline quality --config baseline/quality.yaml [--at …] [--only 檢查,…]
  python3 -m aiops_baseline chronic --config baseline/chronic.yaml [--at …]
  python3 -m aiops_baseline feedback pending|add|list|summary|suggest   值班回饋（真的／誤報／根因錯／漏報）
  python3 -m aiops_baseline incident add|list|resolve                   事故紀錄

train ：讀過去 N 週的資料，每條序列、每個時段學出中位數與波動範圍，寫進 ES 的 aiops-baseline。
detect：讀最近一段時間的資料，跟 baseline 比，超出範圍的寫進 aiops-anomalies，並印出來。
        跳一次是 warning，同一條序列連續兩個時間窗超標（或連續超標滿 30 分鐘）才是 anomaly。
        預設 30 秒一點（跟收值一樣），每 30 秒跑一次：連續兩個 30 秒（約 1 分鐘）就是 anomaly。
tune  ：調整某條序列、某些對象的粒度、z、min_abs（寫進 aiops-tuning；之後 feedback 也寫這裡），下次 train／detect 生效。
adjust：排除時段（事故、維護、壓測不學）、重置點（換版後只學新的）、週期放寬（月底批次放寬或不報），
        也寫 aiops-tuning（aiops_baseline/adjust.py）。status: proposed 的要 approve 才生效。
rules ：狀態變化、離極限多遠（不用學）。
peer  ：跟兄弟比，同一個服務的 Pod 互相比，抓只有一個 Pod 壞掉（不用學）。
feedback：值班對每筆異常的判斷，統計誤報率；誤報累積多次才提放寬門檻的建議（proposed，人核准才生效）。
incident：事故紀錄（時間、服務、根因、處置）；--exclude-training 同時提一筆「這段不學」（proposed）。
chronic：慢性病清單（天天噴錯、沒人修的前 10 名，附負責人），每週一次；寫 aiops-chronic。
quality：資料品質檢查（路由沒參數化、必填欄位缺、資料停了、落到 unassigned 變多），餵給 baseline 之前先看資料本身。
--end、--at 用來回測（拿過去某個時間點當成「現在」）；不給就是現在。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

import pandas as pd
import yaml

from aiops_common.es import get_field

from .es import (ANOMALY_INDEX, ANOMALY_MAPPING, BASELINE_INDEX, BASELINE_MAPPING, ES, doc_id, fetch,
                 load_baseline, load_group_baselines, value_kind)
from . import adjust as A
from . import chronic as C
from . import feedback as F
from . import peer as P
from . import quality as Q
from . import rules as R
from . import trace as T
from .model import (CONFIDENCE, RESOLUTIONS, DetectOptions, TrainOptions, mask_thin, pick_resolution, score,
                    group_of, mark_status, override_for, resolutions_from, score_poisson, to_freq, train,
                    train_groups)


def keys_of(spec: dict) -> list[str]:
    """序列依什麼切：有 keys（統一的名字，例如 service、method、route）用 keys，否則就是 by 的欄位路徑。"""
    return spec.get("keys") or spec["by"]


_freq = to_freq


def _now(arg: str | None) -> pd.Timestamp:
    return pd.Timestamp(arg) if arg else pd.Timestamp.now(tz="UTC")


def _v(x):
    """DataFrame 合併後空的欄位是 NaN，ES 不收：轉成 None。"""
    return None if isinstance(x, float) and pd.isna(x) else x


def save_baseline(es, name: str, by: list[str], b: pd.DataFrame, start, end, had_data: bool = False) -> None:
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
    es.bulk_index(BASELINE_INDEX, docs)
    if docs or had_data:   # ES 查不到任何資料（例如停機）就不刪，保留舊的
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


def apply_resolution_overrides(res: pd.DataFrame, spec: dict, keys: list[str], overrides: list[dict]) -> pd.DataFrame:
    """使用者設定、tune、feedback 指定的粒度，蓋過自動選的。"""
    if res.empty or not overrides:
        return res
    res = res.copy()
    for i, k in zip(res.index, res[keys].to_dict("records")):
        if (r := override_for(spec["name"], k, overrides).get("resolution")):
            res.at[i, "resolution"] = r
    return res


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
    res = pick_resolution(hist[keys + ["ts", "n"]], keys, ad.get("min_events", {}).get(kind, 20 if kind == "rate" else 10),
                          base=base, resolutions=resolutions_from(ad.get("resolutions", RESOLUTIONS), spec.get("min_resolution")),
                          coverage=ad.get("coverage", 0.3))
    return res


def _keep_upgraded(es, docs: list) -> None:
    """不降級：同一個時間窗上一輪已經判成 anomaly（當時看得到前一個時間窗），這一輪查的範圍沒包含前一個時間窗、
    只能判成 warning 時，保留 anomaly。"""
    ids = [i for i, d in docs if d["status"] == "warning"]
    if not ids:
        return
    got = es.req("POST", f"/{ANOMALY_INDEX}/_mget?_source_includes=status,streak", {"ids": ids}).get("docs", [])
    was = {g["_id"]: g["_source"] for g in got if g.get("found") and g["_source"].get("status") == "anomaly"}
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
        docs.append((doc_id(name, keys, r["ts"]), {
            "@timestamp": r["ts"].isoformat(), "series": name, "keys": keys, "value": r["value"],
            "median": r["median"], "expected_low": r["expected_low"], "expected_high": r["expected_high"],
            "z": r["z"], "level": level, "direction": direction, "detected_at": detected_at,
            "resolution": resolution, "method": _v(r.get("baseline_method")) or "mad", "baseline_source": source,
            "confidence": CONFIDENCE.get(level, "low"), "status": status,
            **({"streak": int(r["streak"])} if _v(r.get("streak")) is not None else {}),
            **({"p": r["p"]} if "p" in r else {})}))
    if docs:
        _keep_upgraded(es, docs)
        es.bulk_index(ANOMALY_INDEX, docs)
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


def cmd_train(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(BASELINE_INDEX, BASELINE_MAPPING)
    interval = cfg.get("interval", "5m")
    end = _now(args.end).floor(_freq(interval))
    start = end - pd.Timedelta(weeks=args.weeks or cfg.get("history_weeks", 4))
    # 5 分鐘以下的細粒度只學最近 fine_history（30 秒一點 4 週是 8 萬點；14 天還夠「同星期幾 × 同小時」用）
    fh = str(cfg.get("fine_history", "14d"))
    fine_start = max(start, end - pd.Timedelta(fh[:-1] + "D" if fh.endswith("d") else fh))
    fine = lambda r: pd.Timedelta(_freq(r)) < pd.Timedelta("5min")
    topts = TrainOptions(**cfg.get("train", {}))
    overrides = load_overrides(es, cfg)
    adjs = data_adjustments(es, cfg, end)
    print(f"學習區間 {start.tz_convert(topts.timezone):%Y-%m-%d %H:%M} ～ {end.tz_convert(topts.timezone):%Y-%m-%d %H:%M}"
          f"（{topts.timezone}，{interval} 一點；5 分鐘以下的粒度學最近 {cfg.get('fine_history', '14d')}）")
    if adjs:
        print(f"  {len(adjs)} 筆排除時段、重置點、週期放寬生效中")
    for spec in cfg["series"]:
        keys = keys_of(spec)
        # 排除時段、重置點之前、要排除的週期時段：這些點不學（adjust.py）
        hist_start = fine_start if fine(interval) else start
        hist = fetch(es, spec, hist_start, end, interval)
        had_data = not hist.empty
        hist, dropped = A.train_filter(hist, keys, spec["name"], adjs, topts.timezone)
        excluded = lambda df: A.train_filter(df, keys, spec["name"], adjs, topts.timezone)[0]
        res = apply_resolution_overrides(series_resolutions(cfg, spec, hist, keys), spec, keys, overrides)
        parts = []
        for r, ks in res.dropna(subset=["resolution"]).groupby("resolution"):
            r_start = fine_start if fine(r) else start
            # 百分位不能從細的合併，粗的要重查
            h = hist if (r == interval and r_start == hist_start) else excluded(fetch(es, spec, r_start, end, r))
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
            h = excluded(fetch(es, spec, start, end, coarsest)).merge(sparse[keys], on=keys)
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
        save_baseline(es, spec["name"], keys, pd.concat([b, gb], ignore_index=True), start, end, had_data=had_data)
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


def with_status(cfg: dict, s: pd.DataFrame, keys: list[str], step: str, start) -> pd.DataFrame:
    """超標的點分 warning（跳一次）／anomaly（連續）。只回傳 start 之後的點：start 之前那一個時間窗是多查的，
    用來知道「前一個時間窗有沒有超標」，不重寫（它在上一輪已經判斷過）。"""
    st = cfg.get("status", {})
    out = mark_status(s, keys, step, st.get("confirm_points", 2), st.get("confirm_span", "30m"))
    return out[out["ts"] >= start]


TUNING_INDEX = "aiops-tuning"


TUNING_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "kind": {"type": "keyword"}, "series": {"type": "keyword"}, "match": {"type": "flattened"},
    "resolution": {"type": "keyword"}, "z": {"type": "double"}, "min_abs": {"type": "double"},
    "start": {"type": "date"}, "end": {"type": "date"}, "reset_at": {"type": "date"}, "warmup": {"type": "keyword"},
    "apply_to": {"type": "keyword"},
    "schedule": {"type": "flattened"}, "effect": {"type": "flattened"},
    "status": {"type": "keyword"}, "source": {"type": "keyword"}, "expires_at": {"type": "date"},
    "reason": {"type": "text"}, "by": {"type": "keyword"}, "at": {"type": "date"}}}}


def load_adjustments(es, cfg: dict) -> list[dict]:
    """全部調整（四種 kind）：設定檔（overrides、adjustments_file）＋ aiops-tuning（tune、adjust 指令、之後的 learning loop）。"""
    return A.load(es, TUNING_INDEX, cfg)


def load_overrides(es, cfg: dict) -> list[dict]:
    """粒度、z、min_abs 的調整（kind: override；舊資料沒有 kind 也是），生效中的，後面蓋前面。"""
    return A.active(load_adjustments(es, cfg), pd.Timestamp.now(tz="UTC"), kinds=("override",))


def data_adjustments(es, cfg: dict, now) -> list[dict]:
    """排除時段、重置點、週期放寬，生效中的。"""
    return A.active(load_adjustments(es, cfg), now, kinds=("exclude_window", "reset_point", "recurring_override"))


def cmd_tune(cfg: dict, args) -> int:
    """寫一筆調整進 aiops-tuning（同一條序列＋同一組 match 會覆蓋）；--remove 刪掉；--list 列出。"""
    es = ES()
    es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
    if args.list:
        for o in load_overrides(es, cfg):
            print(A.describe(o))
        return 0
    match = dict(m.split("=", 1) for m in (args.match or []))
    _id = doc_id("tune", args.series, match)
    if args.remove:
        es.req("DELETE", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", ok=(200, 404))
        print(f"已刪除：{args.series} {match}")
        return 0
    doc = {"kind": "override", "series": args.series, "match": match, "resolution": args.resolution, "z": args.z, "min_abs": args.min_abs,
           "reason": args.reason, "by": args.by, "at": pd.Timestamp.now(tz="UTC").isoformat()}
    es.req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", {k: v for k, v in doc.items() if v is not None})
    print(f"已調整：{args.series} {match} → " + "，".join(f"{k}={doc[k]}" for k in ("resolution", "z", "min_abs") if doc[k] is not None)
          + "（粒度下次 train 生效；z、min_abs 下次 detect 生效）")
    return 0


def score_tuned(fn, cur: pd.DataFrame, base: pd.DataFrame, keys: list[str], spec: dict, dopts, tz, overrides) -> pd.DataFrame:
    """依每個對象的調整（z、min_abs）分組打分；沒有調整的用預設。"""
    if cur.empty:
        return cur.assign(anomaly=False)
    ks = cur[keys].drop_duplicates()
    params = []
    for k in ks.to_dict("records"):
        o = override_for(spec["name"], k, overrides)
        params.append((o.get("z", dopts.z), o.get("min_abs", spec.get("min_abs", 0.0))))
    ks = ks.assign(_z=[p[0] for p in params], _min_abs=[p[1] for p in params])
    parts = []
    for (z, min_abs), g in ks.groupby(["_z", "_min_abs"]):
        c = cur.merge(g[keys], on=keys)
        parts.append(fn(c, base, keys, direction=spec.get("direction", "both"), min_abs=min_abs,
                        opts=dataclasses.replace(dopts, z=z), tz=tz))
    return pd.concat(parts, ignore_index=True)


def borrow_resolution(cfg: dict) -> str:
    """借來的 baseline 用多粗的粒度判斷：預設最粗的（本來就是可信度低的判斷，用粗一點的窗比較穩）。"""
    return cfg.get("borrowing", {}).get("resolution") or (cfg.get("adaptive") or {}).get("resolutions", RESOLUTIONS)[-1]


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
              min_abs=spec.get("min_abs", 0.0), opts=bz, tz=tz)
    s, _ = A.detect_filter(s, keys, spec["name"], list(adjs), tz)
    s = with_status(cfg, s.merge(pick, on=keys, how="left"), keys, r, start_r)
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
    overrides = load_overrides(es, cfg)
    adjs = data_adjustments(es, cfg, now)
    # 剛結束的時間窗可能還沒收齊（Collector 批次送、ES refresh），往前退 lag 再看
    lag = pd.Timedelta(_freq(cfg.get("lag", "0s")))
    total = 0
    print(f"偵測區間 {start.tz_convert(tz):%Y-%m-%d %H:%M} ～ {end.tz_convert(tz):%Y-%m-%d %H:%M}（{tz}）")
    for spec in cfg["series"]:
        keys = keys_of(spec)
        base = load_baseline(es, spec["name"], keys)
        if spec.get("borrow") and cfg.get("groups"):
            total += detect_borrowed(es, cfg, spec, base, now, pd.Timedelta(args.window), dopts, tz, adjs)
        if base.empty:
            print(f"  {spec['name']}：沒有 baseline，先跑 train")
            continue
        base["resolution"] = base["resolution"].fillna(interval)
        # 每種粒度各查一次：1 小時一點的序列，看的是最近一個已經結束的整點（每 5 分鐘重跑會覆蓋同一筆，不會重複）
        for r, b in base.groupby("resolution"):
            end_r = (now - lag).floor(_freq(r))
            start_r = end_r - max(pd.Timedelta(args.window), pd.Timedelta(_freq(r)))
            # 多查前一個時間窗：判斷「連續」要知道前一個有沒有超標
            prev_r = start_r - pd.Timedelta(_freq(r))
            cur = fetch(es, spec, prev_r, end_r, r)
            ks = b[keys].drop_duplicates()
            cur = cur.merge(ks, on=keys) if not cur.empty else cur
            if value_kind(spec) == "count" or spec.get("fill_zero"):
                cur = _add_silent(cur, b, keys, prev_r, end_r, r)
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
                s = score_tuned(fn, cm, bm, keys, spec, dopts, tz, overrides).assign(baseline_method=method)
                s, muted = A.detect_filter(s, keys, spec["name"], adjs, tz, baseline=bm)
                if muted:
                    print(f"  {spec['name']}（{r} 一點）：{muted} 個點在排除時段、週期放寬、重置點之後，不算")
                s = with_status(cfg, s, keys, r, start_r)
                total += save_anomalies(es, spec["name"], keys, s, spec.get("direction", "both"), tz, resolution=r)
    print(f"共 {total} 個異常")
    return 0


def cmd_adjust(cfg: dict, args) -> int:
    """排除時段、重置點、週期放寬（adjust.py）：add 寫一筆進 aiops-tuning；list 列出全部（含設定檔、tune 的）；
    approve 把 proposed 改成 active（learning loop 提的建議，人核准才生效）；remove 刪掉。"""
    es = ES()
    es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
    if args.action == "list":
        for a in load_adjustments(es, cfg):
            print(f"{a.get('id') or '（設定檔）'}  {A.describe(a)}")
        return 0
    if args.action in ("approve", "remove"):
        if not args.id:
            print("要給 --id（adjust list 看得到）")
            return 2
        if args.action == "remove":
            es.req("DELETE", f"/{TUNING_INDEX}/_doc/{args.id}?refresh=true", ok=(200, 404))
            print(f"已刪除：{args.id}")
        else:
            es.req("POST", f"/{TUNING_INDEX}/_update/{args.id}?refresh=true",
                   {"doc": {"status": "active", "approved_by": args.by, "approved_at": pd.Timestamp.now(tz="UTC").isoformat()}})
            print(f"已核准：{args.id}")
        return 0
    need = {"exclude_window": ("start", "end"), "reset_point": ("at",), "recurring_override": ("cron", "duration")}
    missing = [f"--{f.replace('_', '-')}" for f in need[args.kind] if not getattr(args, f)]
    if missing:
        print(f"{args.kind} 要給 {'、'.join(missing)}")
        return 2
    match = dict(m.split("=", 1) for m in (args.match or []))
    doc = {"kind": args.kind, "series": args.series, "match": match, "status": args.status, "source": args.source,
           "reason": args.reason, "by": args.by, "expires_at": args.expires, "at": pd.Timestamp.now(tz="UTC").isoformat()}
    if args.kind == "exclude_window":
        doc.update(start=pd.Timestamp(args.start).isoformat(), end=pd.Timestamp(args.end).isoformat(),
                   apply_to=args.apply_to.split(","))
    elif args.kind == "reset_point":
        doc["reset_at"] = pd.Timestamp(args.at).isoformat()
        doc["warmup"] = args.warmup
    else:
        sched = {"cron": args.cron, "duration": args.duration}
        if args.days_of_month:
            sched["days_of_month"] = [int(d) for d in args.days_of_month.split(",")]
        effect = {k: v for k, v in (("z", args.z), ("min_abs", args.min_abs), ("mute", args.mute or None),
                                    ("train", args.train)) if v is not None}
        if not ({"z", "min_abs", "mute"} & set(effect)):
            print("recurring_override 要給 --z、--min-abs 或 --mute")
            return 2
        doc.update(schedule=sched, effect=effect)
    doc = {k: v for k, v in doc.items() if v not in (None, {}, [])} | {"match": match}
    _id = args.id or doc_id(args.kind, args.series, match, doc.get("start"), doc.get("reset_at"), doc.get("schedule"))
    es.req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", doc)
    print(f"已寫入 {_id}：{A.describe(doc)}")
    if doc["status"] == "proposed":
        print("  status 是 proposed，要 adjust approve --id 之後才生效")
    else:
        print("  不學的部分下次 train 生效；不報的部分下次 detect 生效")
    return 0


# ── Check：事故紀錄、值班回饋（aiops_baseline/feedback.py）──

def _anomaly_rows(es, days: int, now) -> pd.DataFrame:
    """最近 days 天的異常（series、target、status、id、時間）。"""
    rows, after = [], None
    while True:
        body = {"size": 5000, "_source": ["@timestamp", "series", "keys", "status", "severity", "level"],
                "query": {"range": {"@timestamp": {"gte": (now - pd.Timedelta(days=days)).isoformat(), "lt": now.isoformat()}}},
                "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}]}
        if after:
            body["search_after"] = after
        hits = es.req("POST", f"/{ANOMALY_INDEX}/_search?ignore_unavailable=true", body).get("hits", {}).get("hits", [])
        rows += [{"id": h["_id"], "series": h["_source"]["series"], "target": F.target_of(h["_source"].get("keys")),
                  # 規則、跟兄弟比、品質檢查沒有 warning／anomaly 之分，都是直接通知的
                  "status": h["_source"].get("status") or "anomaly", "at": h["_source"]["@timestamp"]} for h in hits]
        if len(hits) < 5000:
            break
        after = hits[-1]["sort"]
    return pd.DataFrame(rows, columns=["id", "series", "target", "status", "at"])


def _feedback_rows(es, days: int, now) -> pd.DataFrame:
    hits = es.req("POST", f"/{F.FEEDBACK_INDEX}/_search?ignore_unavailable=true", {
        "size": 10000, "sort": [{"@timestamp": "asc"}],
        "query": {"range": {"@timestamp": {"gte": (now - pd.Timedelta(days=days)).isoformat()}}}}).get("hits", {}).get("hits", [])
    return pd.DataFrame([{"series": h["_source"]["series"], "target": F.target_of(h["_source"].get("keys")),
                          "verdict": h["_source"]["verdict"], "anomaly_id": h["_source"].get("anomaly_id"),
                          "at": h["_source"]["@timestamp"], "by": h["_source"].get("by"), "note": h["_source"].get("note")}
                         for h in hits], columns=["series", "target", "verdict", "anomaly_id", "at", "by", "note"])


def cmd_feedback(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(F.FEEDBACK_INDEX, F.FEEDBACK_MAPPING)
    now = _now(None)
    days = args.days
    if args.action == "pending":
        a, f = _anomaly_rows(es, days, now), _feedback_rows(es, days, now)
        done = set(f["anomaly_id"].dropna())
        a = a[(a["status"] == "anomaly") & ~a["id"].isin(done)]
        if args.series:
            a = a[a["series"].str.contains(args.series)]
        print(f"最近 {days} 天還沒人標記的異常 {len(a)} 筆（顯示最新 {args.limit} 筆）：")
        for r in a.sort_values("at", ascending=False).head(args.limit).to_dict("records"):
            print(f"  {r['id']}  {r['at'][:16]}  {r['series']}  {r['target']}")
        return 0
    if args.action == "list":
        f = _feedback_rows(es, days, now)
        for r in f.sort_values("at", ascending=False).head(args.limit).to_dict("records"):
            print(f"  {r['at'][:16]}  {r['verdict']:<16}  {r['series']}  {r['target']}  ({r['by'] or '?'}) {r['note'] or ''}")
        print(f"共 {len(f)} 筆")
        return 0
    if args.action == "summary":
        a, f = _anomaly_rows(es, days, now), _feedback_rows(es, days, now)
        print("\n".join(F.format_summary(F.summarize(a, f, min_false_positive=args.min_false_positive), days)))
        return 0
    if args.action == "suggest":
        s = F.summarize(_anomaly_rows(es, days, now), _feedback_rows(es, days, now), min_false_positive=args.min_false_positive)
        z0 = DetectOptions(**cfg.get("detect", {})).z
        existing = {(a.get("series"), json.dumps(a.get("match"), sort_keys=True)) for a in load_adjustments(es, cfg)
                    if (a.get("kind") or "override") == "override" and a.get("z")}
        n = 0
        for r in s["candidates"].to_dict("records"):
            sug = F.suggest_override(r["series"], r["target"], r["false_positive"], z0)
            if (sug["series"], json.dumps(sug["match"], sort_keys=True)) in existing:
                print(f"  已經有調整或建議：{r['series']} {r['target']}")
                continue
            sug.update({"at": now.isoformat(), "by": "feedback"})
            _id = doc_id("suggest", sug["series"], sug["match"])
            es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
            es.req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", sug)
            print(f"  建議 {_id}：{A.describe(sug)}")
            n += 1
        print(f"{n} 筆建議（status: proposed，用 adjust approve --id … 核准才生效）；"
              f"誤報不到 {args.min_false_positive} 次的不提")
        return 0
    # add
    if args.verdict not in F.VERDICTS:
        print(f"--verdict 要是 {'、'.join(F.VERDICTS)}")
        return 2
    doc = {"@timestamp": now.isoformat(), "verdict": args.verdict, "note": args.note, "by": args.by,
           "incident_id": args.incident, "correct_root_cause": args.root_cause}
    if args.anomaly:
        got = es.req("GET", f"/{ANOMALY_INDEX}/_doc/{args.anomaly}", ok=(200, 404))
        if not got.get("found"):
            print(f"找不到異常 {args.anomaly}（feedback pending 看得到 id）")
            return 2
        src = got["_source"]
        doc.update(anomaly_id=args.anomaly, anomaly_at=src["@timestamp"], series=src["series"], keys=src.get("keys") or {})
    elif args.verdict == "missed" and args.series:
        doc.update(series=args.series, keys=dict(m.split("=", 1) for m in (args.match or [])), anomaly_at=args.at)
    else:
        print("要給 --anomaly <id>；漏報（missed）給 --series、--match、--at")
        return 2
    doc = {k: v for k, v in doc.items() if v not in (None, "")}
    _id = doc_id("feedback", doc.get("anomaly_id") or [doc["series"], doc.get("keys"), doc.get("anomaly_at")], args.by or "")
    es.req("PUT", f"/{F.FEEDBACK_INDEX}/_doc/{_id}?refresh=true", doc)   # 同一個人對同一筆再標記會覆蓋
    print(f"已記錄：{args.verdict}  {doc['series']}  {F.target_of(doc.get('keys'))}")
    return 0


def cmd_incident(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(F.INCIDENT_INDEX, F.INCIDENT_MAPPING)
    now = _now(None)
    if args.action == "list":
        hits = es.req("POST", f"/{F.INCIDENT_INDEX}/_search?ignore_unavailable=true",
                      {"size": 200, "sort": [{"start": "desc"}]}).get("hits", {}).get("hits", [])
        for h in hits:
            i = h["_source"]
            print(f"  {h['_id']}  [{i.get('status')}] {i['start'][:16]}～{i['end'][:16]}  {i.get('title')}  "
                  f"服務={','.join(i.get('services') or []) or '—'}  根因={i.get('root_cause') or '—'}")
        print(f"共 {len(hits)} 件")
        return 0
    if args.action == "resolve":
        es.req("POST", f"/{F.INCIDENT_INDEX}/_update/{args.id}?refresh=true",
               {"doc": {"status": "resolved", "resolved_at": now.isoformat(), "action": args.action_taken,
                        "root_cause": args.root_cause}})
        print(f"已結案：{args.id}")
        return 0
    if not (args.title and args.start and args.end):
        print("要給 --title、--start、--end")
        return 2
    doc = {"@timestamp": now.isoformat(), "title": args.title, "start": pd.Timestamp(args.start).isoformat(),
           "end": pd.Timestamp(args.end).isoformat(), "services": args.service or [], "severity": args.severity,
           "status": "open", "root_cause": args.root_cause, "action": args.action_taken, "by": args.by,
           "anomaly_ids": args.anomaly or []}
    doc = {k: v for k, v in doc.items() if v not in (None, "", [])}
    _id = args.id or doc_id("incident", doc["title"], doc["start"])
    es.req("PUT", f"/{F.INCIDENT_INDEX}/_doc/{_id}?refresh=true", doc)
    print(f"已記錄事故 {_id}：{args.title}（{doc['start'][:16]}～{doc['end'][:16]}）")
    if args.exclude_training:
        es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
        for sug in F.suggest_exclude({**doc, "id": _id}):
            sid = doc_id("incident-exclude", _id, sug.get("match"))
            es.req("PUT", f"/{TUNING_INDEX}/_doc/{sid}?refresh=true", {**sug, "at": now.isoformat(), "by": args.by or "incident"})
            print(f"  建議 {sid}：{A.describe(sug)}")
        print("  status 是 proposed，用 adjust approve --id … 核准後，下次 train 才不學這段")
    return 0


# ── 慢性病清單（aiops_baseline/chronic.py、baseline/chronic.yaml）──

CHRONIC_INDEX = "aiops-chronic"
CHRONIC_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "week": {"type": "keyword"}, "source": {"type": "keyword"}, "rank": {"type": "integer"},
    "keys": {"type": "flattened"}, "days": {"type": "integer"}, "total": {"type": "long"}, "per_day": {"type": "double"},
    "first_day": {"type": "date"}, "last_day": {"type": "date"}, "owner": {"type": "keyword"}, "message": {"type": "text"}}}}


def cmd_chronic(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(CHRONIC_INDEX, CHRONIC_MAPPING)
    tz = cfg.get("timezone", "Asia/Taipei")
    now = _now(args.at)
    days = cfg.get("days", 7)
    # 看最近 days 個「完整的日子」（當地時間）：今天還沒過完，不算
    end = now.tz_convert(tz).normalize().tz_convert("UTC")
    start = end - pd.Timedelta(days=days)
    week = f"{end.tz_convert(tz) - pd.Timedelta(days=1):%G-W%V}"
    from .sources import load_owners
    owners = load_owners(cfg)
    docs, lines = [], [f"# 慢性病清單 {week}（{start.tz_convert(tz):%m/%d}～{(end - pd.Timedelta(days=1)).tz_convert(tz):%m/%d}，"
                       f"{days} 天裡至少 {cfg.get('min_days', 6)} 天有）", ""]
    for src in cfg["sources"]:
        if src["type"] == "log":
            daily = C.fetch_log_daily(es, src, start, end, tz)
            keys = src.get("keys") or [f.split(".")[-1] for f in src["by"]]
        elif src["type"] == "anomalies":
            daily = C.fetch_anomaly_daily(es, ANOMALY_INDEX, src, start, end, tz)
            keys = ["series", "target"]
        else:
            raise ValueError(f"不認得的來源種類：{src['type']}")
        found = C.find_chronic(daily, keys, min_days=src.get("min_days", cfg.get("min_days", 6)),
                               min_per_day=src.get("min_per_day", 1), top=src.get("top", cfg.get("top", 10)))
        lines += [f"## {src.get('title', src['name'])}（{len(found)} 個）", ""]
        if not found.empty:
            lines += ["| # | 對象 | 天數 | 每天約 | 共 | 負責人 |", "|---|---|---|---|---|---|"]
        for rank, r in enumerate(found.to_dict("records"), 1):
            if src["type"] == "anomalies":
                target = json.loads(r["target"])
                key = {"series": r["series"], **target}
                who = f"{r['series']}：" + "、".join(f"{v}" for v in target.values())
            else:
                key = {k: _v(r[k]) for k in keys}
                who = "、".join(str(v) if v is not None else "（無）" for v in key.values())
            owner = C.owner_of(key, owners) or "未指定"
            msg = C.describe(src, r, keys, days) if src["type"] == "log" else (
                f"{src.get('title', src['name'])}：{who}，{days} 天裡 {int(r['days'])} 天有")
            docs.append((doc_id("chronic", week, src["name"], key), {
                "@timestamp": now.isoformat(), "week": week, "source": src["name"], "rank": rank, "keys": key,
                "days": int(r["days"]), "total": int(r["total"]), "per_day": float(r["per_day"]),
                "first_day": str(r["first_day"]), "last_day": str(r["last_day"]), "owner": owner, "message": msg}))
            lines.append(f"| {rank} | {who} | {int(r['days'])} | {r['per_day']:.0f} | {int(r['total'])} | {owner} |")
        lines.append("")
    if docs:
        es.bulk_index(CHRONIC_INDEX, docs)
    print("\n".join(lines))
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
        r_start = now - pd.Timedelta(_freq(rule["lookback"])) if rule.get("lookback") else start
        hits = R.evaluate(rule, R.fetch(es, rule, r_start, now), now)
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


# ── 資料品質檢查（aiops_baseline/quality.py、baseline/quality.yaml）──

def quality_hits(es, c: dict, now, window, ref_offset) -> list[dict]:
    """跑一個檢查，回傳觸發的對象（Q.eval_* 的結果）。"""
    start, rs, re_ = now - window, now - window - ref_offset, now - ref_offset
    keys, t = Q._keys(c), c["type"]
    if t == "cardinality":
        return Q.eval_cardinality(Q.fetch_cardinality(es, c, start, now), Q.fetch_cardinality(es, c, rs, re_, 0), keys,
                                  max_values=c.get("max_values"), ratio=c.get("ratio", 3.0),
                                  min_increase=c.get("min_increase", 50), id_share_min=c.get("id_share", 0.5))
    if t == "missing":
        return Q.eval_missing(Q.fetch_missing(es, c, start, now), Q.fetch_missing(es, c, rs, re_), keys, c["fields"],
                              min_docs=c.get("min_docs", 100), max_missing=c.get("max_missing"),
                              max_increase=c.get("max_increase"))
    if t == "stopped":
        silent = pd.Timedelta(_freq(c.get("silent", "10m")))
        lookback = pd.Timedelta(_freq(c.get("lookback", "1h")))
        return Q.eval_stopped(Q.fetch_counts(es, c, now - lookback, now - silent), Q.fetch_counts(es, c, now - silent, now),
                              keys, min_docs=c.get("min_docs", 10))
    if t == "present":
        return Q.eval_present(Q.fetch_counts(es, c, start, now), keys, min_docs=c.get("min_docs", 1))
    if t == "share":
        return Q.eval_share(Q.count(es, c["part_index"], c, start, now), Q.count(es, c["index"], c, start, now),
                            Q.count(es, c["part_index"], c, rs, re_), Q.count(es, c["index"], c, rs, re_),
                            min_docs=c.get("min_docs", 100), max_share=c.get("max_share"),
                            min_increase=c.get("min_increase", 0.1))
    if t == "lag":
        return Q.eval_lag(Q.fetch_lag(es, c, start, now), keys, stat=c.get("stat", "p95"),
                          max_seconds=c.get("max_seconds", 60), min_docs=c.get("min_docs", 50),
                          absolute=c.get("absolute", False))
    raise ValueError(f"不認得的檢查種類：{t}")


def cmd_quality(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    es.req("PUT", f"/{ANOMALY_INDEX}/_mapping", {"properties": RULE_FIELDS})
    now = _now(args.at)
    slot = now.floor(_freq(cfg.get("interval", "10m")))
    ref_offset = pd.Timedelta(_freq(cfg.get("ref_offset", "24h")))
    names = set(args.only.split(",")) if args.only else None
    docs, fired, checked = [], 0, 0
    for c in cfg["checks"]:
        if names and c["name"] not in names:
            continue
        checked += 1
        window = pd.Timedelta(_freq(c.get("window", cfg.get("window", "1h"))))
        hits = quality_hits(es, c, now, window, ref_offset)
        for h in hits:
            keys = {**{k: v for k, v in h["key"].items() if v is not None}, **({"field": h["field"]} if "field" in h else {})}
            msg = Q.describe(c, h)
            docs.append((doc_id(c["name"], keys, slot), {
                "@timestamp": slot.isoformat(), "series": c["name"], "rule": c["name"], "keys": keys,
                "value": h["value"], "median": _v(h.get("ref")), "expected_low": None, "expected_high": None, "z": None,
                "level": "quality", "direction": "up", "severity": c.get("severity", "warning"), "message": msg,
                "detected_at": pd.Timestamp.now(tz="UTC").isoformat()}))
            print(f"  [{c.get('severity', 'warning')}] {msg}")
        fired += len(hits)
    if docs:
        es.bulk_index(ANOMALY_INDEX, docs)
    print(f"{slot.tz_convert('Asia/Taipei'):%m-%d %H:%M}：{checked} 個檢查，觸發 {fired} 個")
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
    tu = sub.add_parser("tune", help="調整某條序列、某些對象的粒度、z、min_abs（寫進 aiops-tuning）")
    tu.add_argument("--config", default="baseline/series.yaml")
    tu.add_argument("--series", help="序列名稱（可用正規表示式，例如 http\\..*）")
    tu.add_argument("--match", nargs="*", help="對象，例如 service=^mes- route=/lots（正規表示式）")
    tu.add_argument("--resolution", help="固定粒度，例如 5m")
    tu.add_argument("--z", type=float)
    tu.add_argument("--min-abs", dest="min_abs", type=float)
    tu.add_argument("--reason", help="為什麼調（例如：誤報多、月底批次）")
    tu.add_argument("--by", help="誰調的（使用者、feedback）")
    tu.add_argument("--remove", action="store_true")
    tu.add_argument("--list", action="store_true")
    ad = sub.add_parser("adjust", help="排除時段、重置點、週期放寬（寫進 aiops-tuning）")
    ad.add_argument("action", choices=["add", "list", "approve", "remove"])
    ad.add_argument("--config", default="baseline/series.yaml")
    ad.add_argument("--kind", choices=["exclude_window", "reset_point", "recurring_override"], default="exclude_window")
    ad.add_argument("--id", help="指定 id（同 id 再寫會覆蓋）；approve、remove 必填")
    ad.add_argument("--series", help="哪些序列（正規表示式，不給就是全部）")
    ad.add_argument("--match", nargs="*", help="對象，例如 service=^mes-api$（正規表示式；欄位可用別名：service、namespace、pod…）")
    ad.add_argument("--start", help="exclude_window：開始（ISO 時間，例如 2026-10-20T02:00:00+08:00）")
    ad.add_argument("--end", help="exclude_window：結束")
    ad.add_argument("--apply-to", dest="apply_to", default="train", help="exclude_window：train（不學，預設）、train,detect（也不報）")
    ad.add_argument("--at", help="reset_point：從這個時間點起重新學")
    ad.add_argument("--warmup", help="reset_point：重置後多久內不判斷（預設 24h）")
    ad.add_argument("--cron", help="recurring_override：開始時間（5 欄 cron，台北時間），例如 '0 20 * * *'")
    ad.add_argument("--duration", help="recurring_override：持續多久，例如 4h")
    ad.add_argument("--days-of-month", dest="days_of_month", help="recurring_override：哪幾天，-1 是每月最後一天（逗號分開）")
    ad.add_argument("--z", type=float, help="recurring_override：這段時間 z 至少要多少才算")
    ad.add_argument("--min-abs", dest="min_abs", type=float, help="recurring_override：這段時間至少差多少才算")
    ad.add_argument("--mute", action="store_true", help="recurring_override：這段時間不報")
    ad.add_argument("--train", choices=["keep", "exclude"], help="recurring_override：這段時間要不要學（預設 keep）")
    ad.add_argument("--status", choices=["active", "proposed"], default="active")
    ad.add_argument("--source", default="manual", help="manual、feedback、cicd")
    ad.add_argument("--expires", help="到期時間（選填）")
    ad.add_argument("--reason")
    ad.add_argument("--by")
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
    fb = sub.add_parser("feedback", help="值班回饋：pending（還沒標記的）、add、list、summary（誤報統計）、suggest（誤報多次→建議放寬）")
    fb.add_argument("action", choices=["pending", "add", "list", "summary", "suggest"])
    fb.add_argument("--config", default="baseline/series.yaml")
    fb.add_argument("--anomaly", help="add：異常的 id（pending 看得到）")
    fb.add_argument("--verdict", help="add：true_positive（真的）、false_positive（誤報）、wrong_root_cause（根因錯）、missed（漏報）")
    fb.add_argument("--note")
    fb.add_argument("--by")
    fb.add_argument("--incident", help="add：屬於哪件事故")
    fb.add_argument("--root-cause", dest="root_cause", help="add：正確的根因（wrong_root_cause 時）")
    fb.add_argument("--series", help="add missed：哪條序列；pending：序列名稱包含")
    fb.add_argument("--match", nargs="*", help="add missed：對象，例如 service=mes-api")
    fb.add_argument("--at", help="add missed：漏報的時間")
    fb.add_argument("--days", type=int, default=28)
    fb.add_argument("--limit", type=int, default=50)
    fb.add_argument("--min-false-positive", dest="min_false_positive", type=int, default=3,
                    help="同一個對象誤報幾次才列入、才提建議（預設 3）")
    inc = sub.add_parser("incident", help="事故紀錄：add、list、resolve")
    inc.add_argument("action", choices=["add", "list", "resolve"])
    inc.add_argument("--config", default="baseline/series.yaml")
    inc.add_argument("--id")
    inc.add_argument("--title")
    inc.add_argument("--start")
    inc.add_argument("--end")
    inc.add_argument("--service", nargs="*", help="影響的服務")
    inc.add_argument("--severity", help="例如 sev1、sev2")
    inc.add_argument("--root-cause", dest="root_cause")
    inc.add_argument("--action-taken", dest="action_taken", help="處置")
    inc.add_argument("--anomaly", nargs="*", help="相關的異常 id")
    inc.add_argument("--by")
    inc.add_argument("--exclude-training", dest="exclude_training", action="store_true",
                     help="同時提一筆「這段時間不學」的建議（proposed，adjust approve 才生效）")
    ch = sub.add_parser("chronic", help="慢性病清單：天天噴錯、沒人修的（每週）")
    ch.add_argument("--config", required=True)
    ch.add_argument("--at", help="回測：拿過去某個時間點當成現在")
    q = sub.add_parser("quality")
    q.add_argument("--config", required=True)
    q.add_argument("--at", help="回測：拿過去某個時間點當成現在")
    q.add_argument("--only", help="只跑這幾個（逗號分開）")
    args = p.parse_args(argv)
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cmds = {"train": cmd_train, "detect": cmd_detect, "tune": cmd_tune, "trace-collect": cmd_trace_collect,
            "trace-train": cmd_trace_train, "trace-detect": cmd_trace_detect, "rules": cmd_rules, "peer": cmd_peer,
            "quality": cmd_quality, "adjust": cmd_adjust,
            "chronic": cmd_chronic, "feedback": cmd_feedback, "incident": cmd_incident}
    return cmds[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())

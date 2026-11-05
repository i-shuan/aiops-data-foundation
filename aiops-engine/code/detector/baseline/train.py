"""train：學「平常的樣子」（configs/baseline/series.yaml）。每條序列挑粒度、依時段學中位數與 MAD（model.py），寫進 aiops-baseline。"""
from __future__ import annotations

import pandas as pd

from core.es_client import ES, doc_id
from core.timeutil import _freq, _now, _v, keys_of
from sources import as_backend, fetch, open_backend
from sources.es_aggs import value_kind
from sources.schema import BASELINE_INDEX, BASELINE_MAPPING
from detector.adjust import adjust as A
from detector.adjust.tuning import data_adjustments, load_overrides

from .model import RESOLUTIONS, TrainOptions, mask_thin, override_for, pick_resolution, resolutions_from, train, train_groups


def save_baseline(es, name: str, by: list[str], b: pd.DataFrame, start, end, had_data: bool = False) -> None:
    """寫進 aiops-baseline，再刪掉這條序列上一次訓練留下、這次沒有的（例如改了粒度、或 4 週內都沒出現的 API）。
    had_data：這次有查到資料（只是不夠學）。ES 查不到任何資料時不刪，保留舊的。寫與刪由 Store 做（memory、file、es）。"""
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
    as_backend(es).save_baseline(name, docs, trained_at, had_data)

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

def borrow_resolution(cfg: dict) -> str:
    """借來的 baseline 用多粗的粒度判斷：預設最粗的（本來就是可信度低的判斷，用粗一點的窗比較穩）。"""
    return cfg.get("borrowing", {}).get("resolution") or (cfg.get("adaptive") or {}).get("resolutions", RESOLUTIONS)[-1]


def cmd_train(cfg: dict, args, backend=None) -> int:
    # 設定檔沒寫 source 就是 ES；寫了 file／http 就是那個來源（讀）＋ store（寫）；程式直接給 DataFrame 時傳 backend=sources.MemoryBackend(…)
    es = backend or open_backend(cfg, ES)
    es.ensure_index(BASELINE_INDEX, BASELINE_MAPPING)
    interval = cfg.get("interval", "5m")
    end = _now(args.end).floor(_freq(interval))
    start = end - pd.Timedelta(weeks=args.weeks or cfg.get("history_weeks", 4))
    # 5 分鐘以下的細粒度只學最近 fine_history（30 秒一點 4 週是 8 萬點；14 天還夠「同星期幾 × 同小時」用）
    fh = str(cfg.get("fine_history", "14d"))
    fine_start = max(start, end - pd.Timedelta(fh[:-1] + "D" if fh.endswith("d") else fh))
    fine = lambda r: pd.Timedelta(_freq(r)) < pd.Timedelta("5min")
    topts = TrainOptions(**cfg.get("train", {}))
    overrides = load_overrides(es, cfg, end)    # 有沒有過期以這次 train 的時間點判斷（跟排除時段、重置點一樣）
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

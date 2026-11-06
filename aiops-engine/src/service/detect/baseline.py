"""學「平常的樣子」（baseline）與判斷異常。不碰 ES，輸入輸出都是 pandas DataFrame。

方法（docs/rca/README.md 的方法 A）：
  每條時間序列、每個時段（星期幾 × 幾點），取過去幾週同一時段的值，算中位數與中位數絕對偏差（MAD）。
  之後的值離中位數超過 z 倍「穩健標準差」（1.4826 × MAD）就算異常。中位數與 MAD 不受少數極端值影響，
  歷史裡混了幾次事故也不太會把「平常」拉偏。

歷史不夠時往下退（level 欄位記錄用了哪一層，可信度依序降低）：
  dow_hour：同一個星期幾、同一個小時，至少 min_weeks 個不同的日子
  hour    ：每天同一個小時，至少 min_days 個不同的日子
  all     ：不分時段，至少 min_points 個點
都不夠就沒有 baseline，這條序列不判斷。每個時段至少要有 min_slot_points 個值（粒度是 1 小時時，
「同星期幾 × 同小時」4 週只有 4 個值，會自動退到「每天同一小時」）。

粒度（pick_resolution）：每條序列看「每個時間窗裡底下的事件數」（請求數、筆數），從 5m、15m、30m、1h 挑最細、
而且事件數的中位數夠多的。p95 由 3 個請求算出來是雜訊，筆數大多是 0 時 MAD 也是 0，都要放大時間窗。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from service.detect.adjustments import match_keys

MAD_TO_SIGMA = 1.4826  # 常態分布下 MAD 換成標準差的係數

LEVELS = ("dow_hour", "hour", "all")
RESOLUTIONS = ("30s", "1m", "5m", "15m", "30m", "1h")   # 30 秒＝收值的頻率，最細就到這裡
# 用哪一層當「平常」→ 可信度：自己同星期幾同小時最準；不分時段、借別人的最不準
CONFIDENCE = {"dow_hour": "high", "hour": "medium", "all": "low", "group": "low"}


def to_freq(interval: str) -> str:
    """設定檔用 ES 的寫法（5m、1h），pandas 要寫成 5min、1h。"""
    return interval[:-1] + "min" if interval.endswith("m") else interval


@dataclass(frozen=True)
class TrainOptions:
    timezone: str = "Asia/Taipei"  # 週期依工廠的當地時間算（換班、上下班）
    min_weeks: int = 2             # dow_hour：同一個星期幾至少要有幾天
    min_days: int = 3              # hour：同一個小時至少要有幾天
    min_points: int = 50           # all：至少要有幾個點
    min_slot_points: int = 6       # dow_hour、hour：每個時段至少要有幾個值


@dataclass(frozen=True)
class DetectOptions:
    z: float = 5.0            # 離中位數幾倍穩健標準差算異常
    rel_floor: float = 0.05   # 平常幾乎不動的序列（MAD 接近 0），波動至少當成中位數的 5%，避免一點點變化就告警
    abs_floor: float = 1e-9   # 中位數也是 0 時的最小波動
    alpha: float = 0.001      # 卜瓦松：平常的次數下，出現這麼多（或這麼少）的機率小於 alpha 才算異常


def _slots(ts: pd.Series, tz: str) -> pd.DataFrame:
    local = pd.to_datetime(ts, utc=True).dt.tz_convert(tz)
    return pd.DataFrame({"dow": local.dt.dayofweek, "hour": local.dt.hour, "day": local.dt.date})


def _stats(values: pd.Series) -> dict:
    v = values.to_numpy(dtype=float)
    med = float(np.median(v))
    return {
        "median": med,
        "mad": float(np.median(np.abs(v - med))),
        "p05": float(np.percentile(v, 5)),
        "p95": float(np.percentile(v, 95)),
        "n": int(v.size),
    }


def _poisson_stats(values: pd.Series, alpha: float = 0.001) -> dict:
    """很稀少的筆數：學平均次數 λ（存在 median 欄位，mad 是 0）。

    歷史裡一次事故爆量 50 筆，直接平均會把「平常每小時 0.3 次」拉高好幾倍。所以先算平均，
    把「在這個平均下幾乎不可能」（機率 < alpha）的值壓到那個上限，再平均一次，重複到穩定。正常的值不會被壓到。
    λ 至少 0.5 ÷ 值的個數：這個時段過去 4 週一次都沒發生過，不代表機率是 0（不然出現 1 次就報）。
    """
    v = values.to_numpy(dtype=float)
    floor = 0.5 / len(v)
    lam = max(float(v.mean()), floor)
    for _ in range(10):
        capped = np.minimum(v, _poisson_upper(lam, alpha))
        new = max(float(capped.mean()), floor)
        if abs(new - lam) < 1e-6:
            break
        lam = new
    return {"median": lam, "mad": 0.0, "p05": float(np.percentile(v, 5)), "p95": float(np.percentile(v, 95)),
            "n": int(v.size)}


def train(history: pd.DataFrame, keys: list[str], opts: TrainOptions = TrainOptions(), method: str = "mad") -> pd.DataFrame:
    """history：欄位 keys + ["ts", "value"]，每列是一條序列的一個點（value 為 NaN 的點不算）。

    回傳每條序列、每個時段（dow 0=週一 … 6、hour 0～23）一列：keys + dow、hour、level、median、mad、p05、p95、n、
    baseline_method。每個時段各自從 dow_hour → hour → all 往下找第一個樣本夠的層級。
    method：mad（中位數＋MAD）或 poisson（很稀少的筆數，median 欄位存平均次數 λ）。
    """
    _stats_of = _poisson_stats if method == "poisson" else _stats
    h = history.dropna(subset=["value"]).copy()
    if h.empty:
        return pd.DataFrame(columns=keys + ["dow", "hour", "level", "median", "mad", "p05", "p95", "n"])
    h = pd.concat([h.reset_index(drop=True), _slots(h["ts"], opts.timezone).reset_index(drop=True)], axis=1)

    rows = []
    for key_vals, g in h.groupby(keys, sort=False, dropna=False):
        key_vals = key_vals if isinstance(key_vals, tuple) else (key_vals,)
        key_dict = dict(zip(keys, key_vals))
        all_stats = _stats_of(g["value"]) if len(g) >= opts.min_points else None
        by_hour = {hr: gg for hr, gg in g.groupby("hour")}
        by_dow_hour = {k: gg for k, gg in g.groupby(["dow", "hour"])}
        for dow in range(7):
            for hour in range(24):
                gg = by_dow_hour.get((dow, hour))
                if gg is not None and gg["day"].nunique() >= opts.min_weeks and len(gg) >= opts.min_slot_points:
                    rows.append({**key_dict, "dow": dow, "hour": hour, "level": "dow_hour", **_stats_of(gg["value"])})
                    continue
                gg = by_hour.get(hour)
                if gg is not None and gg["day"].nunique() >= opts.min_days and len(gg) >= opts.min_slot_points:
                    rows.append({**key_dict, "dow": dow, "hour": hour, "level": "hour", **_stats_of(gg["value"])})
                    continue
                if all_stats is not None:
                    rows.append({**key_dict, "dow": dow, "hour": hour, "level": "all", **all_stats})
    return pd.DataFrame(rows).assign(baseline_method=method) if rows else pd.DataFrame()


def pick_resolution(volume: pd.DataFrame, keys: list[str], min_events: float, base: str = "5m",
                    resolutions=RESOLUTIONS, coverage: float = 0.5) -> pd.DataFrame:
    """volume：keys + ts + n（base 粒度，n 是那個時間窗裡底下的事件數；NaN＝不知道，例如收集系統停機）。

    每條序列從 resolutions 裡挑最細、而且至少 coverage 比例的時間窗事件數 ≥ min_events 的；都不夠就是 None。
    coverage 不用 0.5（中位數）：很多 API 只有白天有人用，晚上、週末的時間窗本來就少，不該因此整條不判斷；
    事件數不夠的時間窗之後不判斷（mask_thin）。
    沒出現的時間窗算 0（沒有請求），但只從這條序列第一次出現算起（還沒上線不算）。
    回傳 keys + resolution + events（選中的粒度下每窗事件數的中位數）+ covered（事件數夠的時間窗比例）。
    """
    cols = keys + ["resolution", "events", "covered"]
    # 比原始資料細的粒度不能選（5 分鐘一筆的資料切成 30 秒，九成的窗是空的）
    resolutions = [r for r in resolutions if pd.Timedelta(to_freq(r)) >= pd.Timedelta(to_freq(base))]
    v = volume.dropna(subset=["ts"])
    if v.empty:
        return pd.DataFrame(columns=cols)
    last = v["ts"].max()
    rows = []
    for key_vals, g in v.groupby(keys, sort=False, dropna=False):
        key_vals = key_vals if isinstance(key_vals, tuple) else (key_vals,)
        s = g.groupby("ts")["n"].sum(min_count=1)
        s = s.reindex(pd.date_range(s.index.min(), last, freq=to_freq(base)), fill_value=0.0)
        picked, events, covered = None, 0.0, 0.0
        for r in resolutions:
            w = s.resample(to_freq(r)).sum(min_count=1).dropna()
            events = float(w.median()) if not w.empty else 0.0
            covered = float((w >= min_events).mean()) if not w.empty else 0.0
            if covered >= coverage:
                picked = r
                break
        rows.append({**dict(zip(keys, key_vals)), "resolution": picked, "events": events, "covered": covered})
    return pd.DataFrame(rows, columns=cols)


def mask_thin(df: pd.DataFrame, min_events: float) -> pd.DataFrame:
    """比例、百分位類：事件數不夠的時間窗，值是雜訊（3 個請求的 p95），當成「不知道」——不學、也不判斷。"""
    out = df.copy()
    out.loc[out["n"].fillna(0) < min_events, "value"] = np.nan
    return out


def bucket_floor(median: np.ndarray, buckets) -> np.ndarray:
    """值是從直方圖算出來的（p95）：σ 至少是中位數所在那一格寬度的一半。buckets 是直方圖的邊界（OTel 的 explicit bucket
    boundaries）。p95 平常落在同一格、MAD 是 0，σ 只剩中位數的 5%，跨一格 z 約 23；用半個桶寬，跨一格 z 約 2～3。
    超過最後一個邊界（沒有上限的那一格）用最後一格的寬度。"""
    b = np.asarray(sorted(float(x) for x in buckets), dtype=float)
    edges = np.concatenate([[0.0], b])
    med = np.nan_to_num(np.asarray(median, dtype=float), nan=0.0)
    i = np.clip(np.searchsorted(edges, med, side="right") - 1, 0, len(edges) - 2)
    return np.where(med > b[-1], (b[-1] - b[-2]) / 2 if len(b) > 1 else b[-1] / 2, (edges[i + 1] - edges[i]) / 2)


def score(current: pd.DataFrame, baseline: pd.DataFrame, keys: list[str], direction: str = "both",
          min_abs: float = 0.0, opts: DetectOptions = DetectOptions(), tz: str = "Asia/Taipei",
          buckets=None) -> pd.DataFrame:
    """current：keys + ["ts", "value"]。對每個點找它那個時段的 baseline，算 z 值。

    direction：up（只有變高算異常，例如延遲、錯誤數）、down（只有變低）、both。
    min_abs：跟中位數至少要差這麼多才算（避免量很小的序列，從 1 變 3 就告警）。
    buckets：值是從直方圖算出來的（p95）時給直方圖的邊界，σ 至少半個桶寬（bucket_floor）。
    回傳 current 加上 level、median、mad、expected_low、expected_high、z、anomaly；沒有 baseline 的點 anomaly 為 False。
    """
    c = current.dropna(subset=["value"]).copy().reset_index(drop=True)
    if c.empty:
        return c.assign(level=None, median=np.nan, mad=np.nan, expected_low=np.nan, expected_high=np.nan,
                        z=np.nan, anomaly=False)
    slots = _slots(c["ts"], tz)
    c["dow"], c["hour"] = slots["dow"].to_numpy(), slots["hour"].to_numpy()
    b = baseline[keys + ["dow", "hour", "level", "median", "mad"]]
    m = c.merge(b, on=keys + ["dow", "hour"], how="left")

    floors = [
        MAD_TO_SIGMA * m["mad"].to_numpy(dtype=float),
        opts.rel_floor * np.abs(m["median"].to_numpy(dtype=float)),
        np.full(len(m), opts.abs_floor),
    ]
    if buckets:
        floors.append(bucket_floor(m["median"].to_numpy(dtype=float), buckets))
    sigma = np.maximum.reduce(floors)
    diff = m["value"].to_numpy(dtype=float) - m["median"].to_numpy(dtype=float)
    m["z"] = diff / sigma
    m["expected_low"] = m["median"] - opts.z * sigma
    m["expected_high"] = m["median"] + opts.z * sigma
    over = np.abs(diff) >= min_abs
    if direction == "up":
        hit = (m["z"] >= opts.z) & over
    elif direction == "down":
        hit = (m["z"] <= -opts.z) & over
    else:
        hit = (m["z"].abs() >= opts.z) & over
    m["anomaly"] = hit.fillna(False) & m["median"].notna()
    return m.drop(columns=["dow", "hour"])


def _poisson_sf(k: float, lam: float) -> float:
    """P(X ≥ k)，X ~ Poisson(lam)。用 log 算每一項，k 很大也不會溢位。"""
    k = int(math.ceil(k))
    if k <= 0:
        return 1.0
    cdf = sum(math.exp(-lam + i * math.log(lam) - math.lgamma(i + 1)) for i in range(k))
    return max(0.0, 1.0 - cdf)


def _poisson_cdf(k: float, lam: float) -> float:
    """P(X ≤ k)。"""
    k = int(math.floor(k))
    if k < 0:
        return 0.0
    return min(1.0, sum(math.exp(-lam + i * math.log(lam) - math.lgamma(i + 1)) for i in range(k + 1)))


def _poisson_upper(lam: float, alpha: float) -> int:
    """最少要幾次，機率才小於 alpha：正常範圍的上限（不含）。"""
    k, cdf = 0, 0.0
    while True:
        cdf += math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))
        k += 1
        if 1.0 - cdf < alpha:
            return k


def score_poisson(current: pd.DataFrame, baseline: pd.DataFrame, keys: list[str], direction: str = "both",
                  min_abs: float = 0.0, opts: DetectOptions = DetectOptions(), tz: str = "Asia/Taipei") -> pd.DataFrame:
    """很稀少的筆數：baseline 的 median 是平均次數 λ。這個時間窗出現 x 次，算「平常 λ 次的情況下，至少 x 次」的機率
    （變少看「至多 x 次」），小於 alpha 才算異常。λ 很小時「0 次」本來就很常見，變少自然不會報。

    回傳欄位跟 score 一樣；z 是換算的參考值 (x − λ) ÷ √λ，p 是機率；expected_high 是最少幾次就算異常。
    """
    c = current.dropna(subset=["value"]).copy().reset_index(drop=True)
    slots = _slots(c["ts"], tz) if not c.empty else pd.DataFrame(columns=["dow", "hour"])
    c["dow"], c["hour"] = slots["dow"].to_numpy(), slots["hour"].to_numpy()
    m = c.merge(baseline[keys + ["dow", "hour", "level", "median", "mad"]], on=keys + ["dow", "hour"], how="left")
    p_up, p_down, upper = [], [], []
    for x, lam in zip(m["value"].to_numpy(dtype=float), m["median"].to_numpy(dtype=float)):
        if np.isnan(lam):
            p_up.append(np.nan), p_down.append(np.nan), upper.append(np.nan)
            continue
        p_up.append(_poisson_sf(x, lam))
        p_down.append(_poisson_cdf(x, lam))
        upper.append(_poisson_upper(lam, opts.alpha))
    m["p_up"], m["p_down"], m["expected_high"] = p_up, p_down, upper
    m["expected_low"] = 0.0
    lam = m["median"].to_numpy(dtype=float)
    diff = m["value"].to_numpy(dtype=float) - lam
    m["z"] = diff / np.sqrt(np.maximum(lam, 1e-9))
    over = np.abs(diff) >= min_abs
    up = (m["p_up"] < opts.alpha) & (diff > 0)
    down = (m["p_down"] < opts.alpha) & (diff < 0)
    hit = up if direction == "up" else down if direction == "down" else (up | down)
    m["p"] = np.where(diff > 0, m["p_up"], m["p_down"])
    m["anomaly"] = (hit & over).fillna(False) & m["median"].notna()
    return m.drop(columns=["dow", "hour", "p_up", "p_down"])


def group_of(key: dict, groups: list[dict]) -> list[str]:
    """這條序列屬於哪些組（依設定的順序，由細到粗）。match 的每個欄位都要符合（正規表示式，re.search）。"""
    return [g["name"] for g in groups
            if all(key.get(f) is not None and re.search(pat, str(key[f])) for f, pat in g["match"].items())]


def train_groups(own: pd.DataFrame, keys: list[str], groups: list[dict], min_members: int = 3) -> pd.DataFrame:
    """從成員自己的 baseline 算出每一組的 baseline（借給新 API、量太少的 API 用）。

    own：train 的結果（keys + dow、hour、median、mad…，只用中位數＋MAD 學出來的）。
    每組至少 min_members 個成員；每個時段取成員中位數的中位數、成員 MAD 的中位數。
    回傳 group + dow、hour、level（group）、median、mad、p05、p95、n（成員數）。
    """
    cols = ["group", "dow", "hour", "level", "median", "mad", "p05", "p95", "n"]
    if own.empty or not groups:
        return pd.DataFrame(columns=cols)
    if "baseline_method" in own:
        own = own[own["baseline_method"].fillna("mad") == "mad"]
    members = own[keys].drop_duplicates()
    rows = []
    for g in groups:
        mine = [m for m in members.to_dict("records") if g["name"] in group_of(m, [g])]
        if len(mine) < min_members:
            continue
        sub = own.merge(pd.DataFrame(mine), on=keys)
        for (dow, hour), gg in sub.groupby(["dow", "hour"]):
            if gg[keys].drop_duplicates().shape[0] < min_members:
                continue
            rows.append({"group": g["name"], "dow": dow, "hour": hour, "level": "group",
                         "median": float(gg["median"].median()), "mad": float(gg["mad"].median()),
                         "p05": float(gg["median"].min()), "p95": float(gg["median"].max()), "n": int(len(gg))})
    return pd.DataFrame(rows, columns=cols)


def mark_status(scored: pd.DataFrame, keys: list[str], step: str, confirm_points: int = 2,
                confirm_span: str = "30m", instant_z: float | None = None) -> pd.DataFrame:
    """超標的點分兩級（不用另外存狀態：每次偵測從資料重算，重啟也一樣）：

      warning：超標，但前一個時間窗沒有超標（跳一下，先觀察）
      anomaly：同一條序列、同一個方向，連續超標 confirm_points 個時間窗；
               或連續超標的時間合計 ≥ confirm_span（1 小時一點的序列，一個點就是一小時，不用等兩小時）；
               或偏離 |z| ≥ instant_z（偏太多就不用等：擋沒想到的暴衝）
    中間只要有一個時間窗正常（或沒資料），就從頭算。confirm_points 1 就是一次就算（錯誤率這種一跳就是出事的序列）。

    scored：score／score_poisson 的結果（keys + ts + value + median + anomaly）。回傳加上 status（沒超標是 None）、
    streak（連續超標第幾個）。
    """
    out = scored.copy()
    out["status"], out["streak"] = None, 0
    if out.empty:
        return out
    dt = pd.Timedelta(to_freq(step))
    need = min(confirm_points, max(1, int(np.ceil(pd.Timedelta(to_freq(confirm_span)) / dt))))
    out = out.sort_values("ts").reset_index(drop=True)
    sign = np.sign(out["value"].astype(float) - out["median"].astype(float))
    for _, idx in out.groupby(keys, sort=False, dropna=False).groups.items():
        streak, prev_t, prev_sign = 0, None, 0
        for i in idx:
            t, hit, sg = out.at[i, "ts"], bool(out.at[i, "anomaly"]), sign.iat[i]
            if hit and prev_t is not None and t - prev_t == dt and prev_sign == sg and streak > 0:
                streak += 1
            elif hit:
                streak = 1
            else:
                streak = 0
            out.at[i, "streak"] = streak
            if hit:
                instant = instant_z is not None and "z" in out and pd.notna(out.at[i, "z"]) and abs(float(out.at[i, "z"])) >= instant_z
                out.at[i, "status"] = "anomaly" if streak >= need or instant else "warning"
            prev_t, prev_sign = t, (sg if hit else 0)
    return out


def resolutions_from(resolutions, min_resolution: str | None) -> list[str]:
    """候選粒度，去掉比 min_resolution 細的（例如請求數：收值有抖動，30 秒窗會收到 0 或 2 筆，最細 1 分鐘）。"""
    if not min_resolution:
        return list(resolutions)
    floor = pd.Timedelta(to_freq(min_resolution))
    return [r for r in resolutions if pd.Timedelta(to_freq(r)) >= floor]


OVERRIDE_FIELDS = ("resolution", "z", "min_abs", "confirm_points", "instant_z", "min_count")


def _matching_overrides(series: str, key: dict, overrides: list[dict]):
    """符合這條序列、這個對象的 override，依原本的順序（後面的蓋前面的）。"""
    for o in overrides:
        if (o.get("kind") or "override") != "override":   # 排除時段、重置點、週期放寬由 adjust.py 處理
            continue
        if re.fullmatch(o.get("series") or ".*", series) and match_keys(key, o.get("match")):
            yield o


def override_for(series: str, key: dict, overrides: list[dict]) -> dict:
    """這條序列、這個對象的調整（粒度、z、min_abs、confirm_points、instant_z）：符合的依序套上，後面的蓋前面的。
    來源：series.yaml 的 overrides（使用者設定），再來是 aiops-tuning（tune 指令、之後的 feedback 寫的）。"""
    out = {}
    for o in _matching_overrides(series, key, overrides):
        out.update({k: o[k] for k in OVERRIDE_FIELDS if o.get(k) is not None})
    return out


def effective_for(series: str, key: dict, overrides: list[dict]) -> dict:
    """跟 override_for 同一套規則，但每個參數多記「是哪一筆定的」：{"z": {"value": 7, "source": "<id>", "series": …, "match": …}}。
    source：aiops-tuning 的文件有 id（adjust list 看得到）；設定檔的沒有 id，寫成 series.yaml overrides#<第幾筆>
    （adjustments.yaml 裡沒寫 id 的 override 也排在這個序號裡，在 series.yaml 的後面）。"""
    out, n_cfg = {}, 0
    for o in _matching_overrides(series, key, overrides):
        if not o.get("id"):
            n_cfg += 1
        src = o.get("id") or f"series.yaml overrides#{n_cfg}"
        for k in OVERRIDE_FIELDS:
            if o.get(k) is not None:
                out[k] = {"value": o[k], "source": src, "series": o.get("series"), "match": o.get("match")}
    return out

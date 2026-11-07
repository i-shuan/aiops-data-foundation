"""測試用的記憶體版資料來源：原始列（DataFrame，一列一個事件或一個預先統計好的點）＋序列定義 → 模型的標準輸入，
跟 ES 端的 aggregation（detect/store/series_repo.py 的 _value_aggs、_bucket_value、_bucket_events）同一套定義的 pandas 實作，
同一批資料兩邊算出來要一樣（test_shape、test_values）。tests/fakes.MemoryRepos 用它把 DataFrame 當成 ES 的索引。

原始列的欄位名用 ES 的點號路徑（service.name、event.outcome、transaction.duration.histogram）。
直方圖欄位可以是一格 {"values": […], "counts": […]}，或攤成 <欄位>.values、<欄位>.counts 兩欄（json_normalize 的結果）。

兩種格式（來源設定的 format）：
  events  一列一個事件：依 by、時間窗分組，value 的寫法跟 series.yaml 一樣（count、sum／avg／max／min、percentile、ratio、div、
          hist_count、hist_ratio、hist_ratio_above）
  series  已經是標準輸入（keys 或 by 的欄位 + ts + value [+ n]）：只做時間窗、改名、合併、補 0；同一個時間窗有多列時
          筆數類相加、其他依 n 加權平均（沒有 n 就平均）

filter 支援 ES query DSL 的子集：term、terms、range、exists、prefix、wildcard、match、match_all、bool（filter／must／should／must_not）。
"""
from __future__ import annotations

import fnmatch
import re

import numpy as np
import pandas as pd

from common.clock import pandas_freq
from clean.store.series_repo import COLUMNS, fill_zero

class ShapeError(ValueError):
    pass


# ── filter（ES query DSL 的子集）──

def _col(df: pd.DataFrame, field: str) -> pd.Series | None:
    return df[field] if field in df.columns else None


def _eq(s: pd.Series, v) -> np.ndarray:
    if isinstance(v, str) and not pd.api.types.is_object_dtype(s) and not pd.api.types.is_string_dtype(s):
        return (s.astype(str) == v).to_numpy()
    return (s == v).to_numpy()


def mask(df: pd.DataFrame, q) -> np.ndarray:
    """query → 每列 True／False。欄位不存在：term／terms／exists／range 都是 False（跟 ES 一樣，沒有的欄位不符合）。"""
    n = len(df)
    if not q:
        return np.ones(n, dtype=bool)
    if isinstance(q, list):
        out = np.ones(n, dtype=bool)
        for x in q:
            out &= mask(df, x)
        return out
    (kind, arg), = q.items()
    if kind == "match_all":
        return np.ones(n, dtype=bool)
    if kind == "bool":
        out = np.ones(n, dtype=bool)
        for k in ("filter", "must"):
            if arg.get(k):
                out &= mask(df, arg[k])
        if arg.get("should"):
            sh = arg["should"] if isinstance(arg["should"], list) else [arg["should"]]
            any_ = np.zeros(n, dtype=bool)
            for x in sh:
                any_ |= mask(df, x)
            out &= any_
        if arg.get("must_not"):
            mn = arg["must_not"] if isinstance(arg["must_not"], list) else [arg["must_not"]]
            for x in mn:
                out &= ~mask(df, x)
        return out
    if kind not in ("term", "terms", "exists", "prefix", "wildcard", "match", "range"):
        raise ShapeError(f"filter 不支援 {kind}（支援 term、terms、range、exists、prefix、wildcard、match、match_all、bool）")
    if kind == "exists":
        field, v = arg["field"], None
    else:
        (field, v), = arg.items()
    s = _col(df, field)
    if s is None:
        return np.zeros(n, dtype=bool)
    if kind == "term":
        return _eq(s, v["value"] if isinstance(v, dict) else v) & s.notna().to_numpy()
    if kind == "terms":
        out = np.zeros(n, dtype=bool)
        for x in v:
            out |= _eq(s, x)
        return out & s.notna().to_numpy()
    if kind == "exists":
        return s.notna().to_numpy()
    if kind == "prefix":
        return s.astype(str).str.startswith(str(v["value"] if isinstance(v, dict) else v)).to_numpy() & s.notna().to_numpy()
    if kind == "wildcard":
        pat = v["value"] if isinstance(v, dict) else v
        return np.array([bool(fnmatch.fnmatchcase(str(x), pat)) if pd.notna(x) else False for x in s], dtype=bool)
    if kind == "match":
        text = str(v["query"] if isinstance(v, dict) else v)
        return s.astype(str).str.contains(re.escape(text), case=False, regex=True).to_numpy() & s.notna().to_numpy()
    if kind == "range":
        out = np.ones(n, dtype=bool)
        col = s
        if any(isinstance(b, str) and not str(b).replace(".", "", 1).lstrip("-").isdigit() for b in v.values()):
            col = pd.to_datetime(s, utc=True, errors="coerce")
            bounds = {k: pd.Timestamp(b).tz_localize("UTC") if pd.Timestamp(b).tzinfo is None else pd.Timestamp(b) for k, b in v.items()}
        else:
            col = pd.to_numeric(s, errors="coerce")
            bounds = {k: float(b) for k, b in v.items()}
        for op, b in bounds.items():
            out &= {"gte": col >= b, "gt": col > b, "lte": col <= b, "lt": col < b}[op].to_numpy()
        return out & col.notna().to_numpy()
    raise ShapeError(f"filter 不支援 {kind}")   # 上面已擋，保險


# ── 共用 ──

def _ts(df: pd.DataFrame, time_field: str) -> pd.Series:
    if time_field not in df.columns:
        raise ShapeError(f"原始列沒有時間欄位 {time_field}（有：{list(df.columns)[:12]}…）")
    s = df[time_field]
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_datetime(s, unit="ms", utc=True)
    return pd.to_datetime(s, utc=True, errors="coerce")


def in_window(df: pd.DataFrame, time_field: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """[start, end) 的列，多一欄 ts（UTC）。"""
    if df.empty:
        return df.assign(ts=pd.Series(dtype="datetime64[ns, UTC]"))
    ts = _ts(df, time_field)
    keep = (ts >= start) & (ts < end)
    return df.loc[keep.to_numpy()].assign(ts=ts[keep].to_numpy())


def _hist(df: pd.DataFrame, field: str) -> tuple[list, list] | None:
    """直方圖欄位：一格一個 dict，或攤成 .values／.counts 兩欄。回傳 (每列的 values, 每列的 counts)；不是直方圖回 None。"""
    if field in df.columns:
        col = df[field]
        if col.map(lambda x: isinstance(x, dict) and "counts" in x).any():
            return (col.map(lambda x: x.get("values", []) if isinstance(x, dict) else []).tolist(),
                    col.map(lambda x: x.get("counts", []) if isinstance(x, dict) else []).tolist())
        return None
    if f"{field}.values" in df.columns and f"{field}.counts" in df.columns:
        fix = lambda x: list(x) if isinstance(x, (list, tuple, np.ndarray)) else []
        return df[f"{field}.values"].map(fix).tolist(), df[f"{field}.counts"].map(fix).tolist()
    return None


def _need(df: pd.DataFrame, field: str, name: str) -> pd.Series:
    if field not in df.columns:
        raise ShapeError(f"序列 {name}：原始列沒有欄位 {field}")
    return pd.to_numeric(df[field], errors="coerce")


def _weighted_percentile(values: np.ndarray, counts: np.ndarray, pct: float) -> float | None:
    if counts.sum() <= 0:
        return None
    order = np.argsort(values)
    v, c = values[order], counts[order]
    cum = np.cumsum(c)
    target = pct / 100.0 * cum[-1]
    return float(v[int(np.searchsorted(cum, target, side="left"))])


# ── events → series ──

def _agg(g: pd.DataFrame, value, scale: float, name: str, hist) -> tuple[float | None, float | None]:
    """一個 (對象, 時間窗) 的列 → (value, n)。語意同 es_source._bucket_value、_bucket_events。"""
    doc_count = float(len(g))
    if value == "count":
        return doc_count, doc_count
    (kind, arg), = value.items()
    if kind in ("sum", "avg", "max", "min"):
        s = _need(g, arg, name).dropna()
        if s.empty:
            return None, None
        return float(getattr(s, "mean" if kind == "avg" else kind)()) * scale, None
    if kind == "ratio":
        if not doc_count:
            return None, doc_count
        return float(mask(g, arg).sum()) / doc_count, doc_count
    if kind == "div":
        num = arg["num"] if isinstance(arg["num"], dict) else {"field": arg["num"]}
        den = float(_need(g, arg["den"], name).fillna(0).sum())
        if not den:
            return None, den
        gg = g[mask(g, num["query"])] if num.get("query") else g
        return float(_need(gg, num["field"], name).fillna(0).sum()) / den * scale, den
    if kind == "percentile":
        h = hist(arg["field"])
        if h is not None:
            vals = np.array([x for vs in h[0] for x in vs], dtype=float)
            cnts = np.array([x for cs in h[1] for x in cs], dtype=float)
            return (None if (p := _weighted_percentile(vals, cnts, arg["percent"])) is None else p * scale), float(cnts.sum())
        s = _need(g, arg["field"], name).dropna()
        if s.empty:
            return None, 0.0
        return float(np.percentile(s.to_numpy(dtype=float), arg["percent"])) * scale, float(len(s))
    if kind in ("hist_count", "hist_ratio", "hist_ratio_above"):
        field = arg if kind == "hist_count" else arg["field"]
        h = hist(field)
        if h is None:
            raise ShapeError(f"序列 {name}：{kind} 要直方圖欄位 {field}（一格 {{values, counts}} 或 {field}.values／.counts 兩欄）；"
                             f"原始列是一列一個事件的話，改用 count、ratio、percentile")
        total = float(sum(sum(cs) for cs in h[1]))
        if kind == "hist_count":
            return total, total
        if not total:
            return None, total
        if kind == "hist_ratio":
            sel = mask(g, arg["query"])
            return float(sum(sum(cs) for cs, ok in zip(h[1], sel) if ok)) / total, total
        gt = arg["gt"] / scale   # 門檻換回資料的單位（跟 ES 的 percentile_ranks 一樣）；桶不內插：整格算在一邊
        above = sum(c for vs, cs in zip(h[0], h[1]) for v, c in zip(vs, cs) if v > gt)
        return max(0.0, float(above) / total), total
    raise ShapeError(f"序列 {name}：不認得的 value：{value}")


def _n_of(g: pd.DataFrame, events, value_n, name: str) -> float | None:
    """events: value → n 是 value；events: {sum: 欄位} → 另外算；其他照 value 的種類。"""
    if events == "value":
        return value_n
    if isinstance(events, dict):
        v, _ = _agg(g, events, 1.0, name, lambda f: _hist(g, f))
        return v
    return value_n


def series_from_rows(rows: pd.DataFrame, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str | None,
                     fmt: str = "events", alive: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    """原始列 → keys + ts + value + n（detector/es.fetch 的回傳格式，keys 已改名）。
    fmt events：rows 一列一個事件；fmt series：rows 已經是 keys／by + ts + value [+ n]。
    alive：index 有資料的時間窗（補 0 用；events 格式沒給就從 rows 自己算，跟 ES 的 alive_times 一樣不加 filter）。"""
    name, by = spec.get("name", "?"), spec["by"]
    keys = spec.get("keys") or by
    tf = spec.get("time_field", "@timestamp")
    scale = float(spec.get("scale") or 1.0)
    cols = keys + COLUMNS
    if fmt == "series":
        df = _series_format(rows, spec, start, end, interval, keys, by)
    else:
        df = in_window(rows, tf, start, end)
        if spec.get("filter") and not df.empty:
            df = df[mask(df, spec["filter"])]
        missing = [f for f in by if f not in df.columns]
        if missing and not rows.empty:
            raise ShapeError(f"序列 {name}：原始列沒有 by 的欄位 {missing}（有：{sorted(rows.columns)[:15]}…）")
        if df.empty:
            df = pd.DataFrame(columns=keys + COLUMNS)
        else:
            df = df.assign(ts=df["ts"].dt.floor(pandas_freq(interval)) if interval else end)
            out = []
            for key_vals, g in df.groupby(by + ["ts"], sort=False):
                hist = lambda f, g=g: _hist(g, f)
                v, n = _agg(g, spec["value"], scale, name, hist)
                n = _n_of(g, spec.get("events"), v if spec.get("events") == "value" else n, name)
                out.append({**dict(zip(by + ["ts"], key_vals)), "value": v, "n": n})
            df = pd.DataFrame(out, columns=by + COLUMNS).rename(columns=dict(zip(by, keys)))
    if interval and (spec["value"] == "count" or spec.get("fill_zero")) and not df.empty:
        if alive is None and spec.get("filter") and fmt == "events":
            alive = alive_from_rows(rows, tf, start, end, interval)
        df = fill_zero(df, keys, start, end, interval, alive)
    return df[cols].reset_index(drop=True)


def _series_format(rows, spec, start, end, interval, keys, by) -> pd.DataFrame:
    name = spec.get("name", "?")
    tcol = "ts" if "ts" in rows.columns else spec.get("time_field", "@timestamp")
    df = in_window(rows, tcol, start, end)
    have = [k for k in keys if k in df.columns]
    if len(have) != len(keys):
        if all(f in df.columns for f in by):
            df = df.rename(columns=dict(zip(by, keys)))
        elif not rows.empty:
            raise ShapeError(f"序列 {name}（format series）：要有 {keys} 或 {by}、ts、value 欄位（有：{sorted(rows.columns)[:15]}…）")
    if "value" not in df.columns and not rows.empty:
        raise ShapeError(f"序列 {name}（format series）：沒有 value 欄位")
    if df.empty:
        return pd.DataFrame(columns=keys + COLUMNS)
    df = df.assign(value=pd.to_numeric(df["value"], errors="coerce") * float(spec.get("scale") or 1.0),
                   n=pd.to_numeric(df["n"], errors="coerce") if "n" in df.columns else np.nan,
                   ts=df["ts"].dt.floor(pandas_freq(interval)) if interval else end)
    count_like = spec.get("value") == "count" or spec.get("events") == "value"
    out = []
    for key_vals, g in df.groupby(keys + ["ts"], sort=False):
        if count_like:
            v, n = float(g["value"].sum()), float(g["value"].sum())
        else:
            w = g["n"].fillna(0).to_numpy(dtype=float)
            vals = g["value"].to_numpy(dtype=float)
            v = float(np.average(vals, weights=w)) if w.sum() > 0 else float(np.nanmean(vals))
            n = float(w.sum()) if g["n"].notna().any() else None
        out.append({**dict(zip(keys + ["ts"], key_vals)), "value": v, "n": n})
    return pd.DataFrame(out, columns=keys + COLUMNS)


def alive_from_rows(rows: pd.DataFrame, time_field: str, start, end, interval: str) -> pd.DatetimeIndex:
    """index 裡有任何一列的時間窗（不加 filter）＝收集系統有在送資料。"""
    df = in_window(rows, time_field, start, end)
    if df.empty:
        return pd.DatetimeIndex([], tz="UTC")
    return pd.DatetimeIndex(df["ts"].dt.floor(pandas_freq(interval)).unique())


# ── events → points（規則）──

def _docs(df: pd.DataFrame) -> list[dict]:
    recs = df.drop(columns=["ts"]).to_dict("records")
    return [{k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in r.items()} for r in recs]


def points_from_rows(rows: pd.DataFrame, rule: dict, start: pd.Timestamp, end: pd.Timestamp) -> dict[tuple, list[tuple[pd.Timestamp, dict]]]:
    """原始列 → {by 的值元組: [(ts, 文件), …]}（detector/rules.fetch 的回傳格式）。
    文件是攤平的 dict（欄位名就是點號路徑；core/es.get_field 兩種都讀得到）。join：分母在另一種文件，依同對象、同時間點補進來。"""
    tf = rule.get("time_field", "@timestamp")
    out = _points(rows, rule, rule["field"], rule.get("filter", []), start, end, tf, rule.get("exists"))
    if rule.get("join"):
        j = rule["join"]
        other = _points(rows, rule, j["field"], j.get("filter", []), start, end, tf, None)
        lookup = {(k, t): d.get(j["field"]) for k, pts in other.items() for t, d in pts}
        for k, pts in out.items():
            for t, d in pts:
                if (v := lookup.get((k, t))) is not None:
                    d[j["field"]] = v
    return out


def _points(rows, rule, field, filters, start, end, tf, exists):
    df = in_window(rows, tf, start, end)
    missing = [f for f in rule["by"] if f not in df.columns]
    if missing and not rows.empty:
        raise ShapeError(f"規則 {rule.get('name', '?')}：原始列沒有 by 的欄位 {missing}")
    have = exists or field
    if have not in df.columns or df.empty:
        return {}
    df = df[df[have].notna()]
    if filters:
        df = df[mask(df, filters if isinstance(filters, list) else [filters])]
    df = df.sort_values("ts", kind="stable")
    out: dict[tuple, list] = {}
    for t, d in zip(df["ts"].tolist(), _docs(df)):
        key = tuple(d.get(f) for f in rule["by"])
        out.setdefault(key, []).append((t, d))
    return out


# ── 原始列的載入（檔案、HTTP 回的 JSON）──

def rows_from_records(records) -> pd.DataFrame:
    """JSON（list 或 {"rows": [...]}、{"hits": ...} 不處理）→ 攤平的 DataFrame：巢狀 dict 變成點號欄位；list 保留。"""
    if isinstance(records, dict):
        records = records.get("rows") or records.get("data") or []
    if not records:
        return pd.DataFrame()
    return pd.json_normalize(list(records), sep=".")

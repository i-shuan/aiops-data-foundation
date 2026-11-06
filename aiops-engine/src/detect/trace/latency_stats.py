"""trace 每一段的 baseline：每個 API 的每個請求，平常呼叫每一段幾次、每段花多久、出錯的比例。

為什麼要有（docs/rca/README.md）：N+1（每個請求打同一句 SQL 100 次）、重試風暴（一個請求打下游 4 次）、
快取穿透（平常 5% 的請求才查 DB，現在每個都查）——每一次呼叫都很快，服務的延遲、錯誤率可能只動一點點；
要看「次數」。拖累（簡訊廠商掛了）、熱點鎖則是某一段的時間變長。

三個步驟：
  collect：每 5 分鐘、每個 API（服務的 Server span）抽幾個請求，算每一段的 次數／時間／錯誤，寫進 aiops-trace-segments。
           這份統計很小，可以留很久（trace 原文只留 7 天，統計值跟著 metrics 留）。
  train  ：讀過去幾週的統計，每個「API × 段」× 時段（星期幾 × 小時）學中位數、MAD（跟 metrics 的 baseline 同一套）。
  detect ：collect 最近一段時間，跟 baseline 比，異常寫進 aiops-anomalies。順便檢查時鐘不同步（規則，不用學）。

「段」只算這個服務自己的部分：服務自己的程式（自己的時間），以及它往外的每一個呼叫（等多久就算多久）。
下游服務裡面的事，由下游自己的 API 那份 baseline 管。這樣異常會出在真正變的那個服務。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

import pandas as pd

from shared.infra.es import ES, doc_id
from detect.store.trace import group_traces, segment, self_times
from detect.store.anomaly import Anomaly
from shared.infra.repos import Repos
from shared.scenario import load_scenario
import detect.series_ops.manual_adjust as A
from detect.series_ops.manual_adjust import data_adjustments
from detect.detect_service import save_anomalies, save_baseline, train_summary, with_status
from detect.utils import _freq, _now
import detect.trace.span_merge as M

KEYS = ["entry.service", "entry.name", "segment"]
COLUMNS = ["ts", *KEYS, "requests", "calls", "time_ms", "error_rate", "swallowed_rate", "exception_type"]
METRICS = ("calls", "time_ms", "error_rate", "swallowed_rate")


# ── 計算（不碰 ES）──

def own_part(trace: list[dict], entry: dict) -> list[dict]:
    """這個請求在這個服務裡的部分：entry 底下、同一個服務的 span（含往外呼叫的 Client span），不往下游服務裡面走。"""
    children = defaultdict(list)
    for s in trace:
        if s.get("parent"):
            children[s["parent"]].append(s)
    out, stack = [], [entry]
    while stack:
        s = stack.pop()
        out.append(s)
        stack += [c for c in children.get(s["span_id"], []) if c["service"] == entry["service"]]
    return out


def producer_of(entry: dict, trace: list[dict], linked: dict[str, dict] | None = None) -> dict | None:
    """訊息消費者（Consumer span）對應的生產者（Producer span）：上一層就是生產者（程式裡接上了 trace），
    或用 link 指到的（.NET 自動埋點的 receive：新的 trace，只用 link 指回生產者）。"""
    if entry.get("kind") != "Consumer":
        return None
    by_id = {s["span_id"]: s for s in trace}
    p = by_id.get(entry.get("parent"))
    if p and p["kind"] == "Producer":
        return p
    for sid in entry.get("links") or []:
        p = (linked or {}).get(sid)
        if p and p["kind"] == "Producer":
            return p
    return None


def queue_wait(entry: dict, producer: dict) -> tuple[str, float]:
    """訊息在佇列裡等了多久：消費者拿到訊息的時間 − 生產者送出的時間。回傳（段的名稱, 毫秒）。
    receive（.NET 自動埋點）這一段是「等下一筆訊息」，結束時才拿到訊息，所以用結束時間；process 用開始時間。"""
    got = entry["start_ms"] + entry["dur_ms"] if entry["name"].endswith(" receive") else entry["start_ms"]
    dest = (entry.get("attrs") or {}).get("messaging.destination.name") or entry["name"].split(" ")[0]
    return f"{entry['service']} › 排隊（{dest}）← {producer['service']}", max(0.0, got - (producer["start_ms"] + producer["dur_ms"]))


def request_segments(trace: list[dict], entry: dict, producer: dict | None = None) -> dict[str, dict]:
    """一個請求、每一段：呼叫幾次、花多久、有沒有出錯、有沒有吞掉的例外。
    時間：往外呼叫（Client）算整段（就是在等對方）；其他算自己的時間（扣掉底下的呼叫）。加起來約等於整個請求的時間。
    吞掉的例外：這一段丟過例外（exception），但請求沒有失敗（程式 catch 掉了，例如回空值、回 200）：
      - 這一段的 status 不是錯誤；或
      - 這一段標了錯誤、入口（這個服務收到的請求）卻沒有。Java agent 的 JDBC、HTTP client 丟例外時一定把那一段標成錯誤，
        程式 catch 掉、回 200 就是這個樣子。錯誤一路傳到入口（回 500）的不算吞掉，算錯誤（error_rate）。"""
    st = self_times(trace)
    entry_failed = bool(entry["error"])
    out: dict[str, dict] = {}
    if producer is not None:   # 訊息消費者：多一段「在佇列裡等了多久」
        seg, ms = queue_wait(entry, producer)
        out[seg] = {"calls": 1, "time_ms": ms, "error": False, "swallowed": None}
    for s in own_part(trace, entry):
        # .NET 自動埋點的 receive：時間是「等下一筆訊息」，不是處理，不算
        if s is entry and s["kind"] == "Consumer" and s["name"].endswith(" receive"):
            continue
        seg = segment(s)
        d = out.setdefault(seg, {"calls": 0, "time_ms": 0.0, "error": False, "swallowed": None})
        d["calls"] += 1
        d["time_ms"] += s["dur_ms"] if s["kind"] == "Client" else st[s["span_id"]]
        d["error"] |= bool(s["error"])
        if s.get("exception") and (not s["error"] or not entry_failed):
            d["swallowed"] = s["exception"]
    return out


def clock_skews(trace: list[dict], tolerance_ms: float) -> list[tuple[str, str, float]]:
    """時鐘不同步（規則，不用學）：同步呼叫的對方（Client → 另一個服務的 Server）比呼叫方早開始、或晚結束超過容許值。
    同一台機器上的時間不會倒流；差很多只可能是兩台機器的時鐘不一致。回傳 (呼叫方, 被呼叫方, 差幾毫秒)。"""
    by_id = {s["span_id"]: s for s in trace}
    out = []
    for c in trace:
        p = by_id.get(c.get("parent"))
        if not p or p["kind"] != "Client" or c["kind"] != "Server" or p["service"] == c["service"]:
            continue
        early = p["start_ms"] - c["start_ms"]
        late = (c["start_ms"] + c["dur_ms"]) - (p["start_ms"] + p["dur_ms"])
        skew = max(early, late)
        if skew > tolerance_ms:
            out.append((p["service"], c["service"], skew))
    return out


@dataclass
class Sampled:
    bucket: pd.Timestamp
    trace_id: str
    span_id: str


def summarize(samples: list[Sampled], traces: dict[str, list[dict]], skew_tolerance_ms: float = 500,
              linked: dict[str, dict] | None = None) -> tuple[pd.DataFrame, list[dict]]:
    """抽到的請求 → 每個 (時段, API, 段) 的平均：每個請求呼叫幾次、花多久、出錯比例。
    某個請求沒呼叫某一段，那一段就是 0 次（例如快取命中時沒查 DB），所以平均是除以這個 API 被抽到的請求數。"""
    acc = defaultdict(lambda: {"calls": 0.0, "time_ms": 0.0, "errors": 0, "swallowed": 0, "types": Counter()})
    reqs = defaultdict(int)
    skews: dict[tuple, float] = {}
    for smp in samples:
        t = traces.get(smp.trace_id)
        entry = next((s for s in t or [] if s["span_id"] == smp.span_id), None)
        if entry is None:
            continue
        k = (smp.bucket, entry["service"], entry["name"])
        reqs[k] += 1
        for seg, d in request_segments(t, entry, producer_of(entry, t, linked)).items():
            a = acc[k + (seg,)]
            a["calls"] += d["calls"]
            a["time_ms"] += d["time_ms"]
            a["errors"] += d["error"]
            if d["swallowed"]:
                a["swallowed"] += 1
                a["types"][d["swallowed"]] += 1
    for tid, bucket in {smp.trace_id: smp.bucket for smp in samples}.items():
        for caller, callee, ms in clock_skews(traces.get(tid, []), skew_tolerance_ms):
            key = (bucket, caller, callee)
            skews[key] = max(skews.get(key, 0.0), ms)
    rows = []
    for (b, svc, name, seg), a in acc.items():
        n = reqs[(b, svc, name)]
        rows.append({"ts": b, "entry.service": svc, "entry.name": name, "segment": seg, "requests": n,
                     "calls": a["calls"] / n, "time_ms": a["time_ms"] / n, "error_rate": a["errors"] / n,
                     "swallowed_rate": a["swallowed"] / n,
                     "exception_type": a["types"].most_common(1)[0][0] if a["types"] else None})
    df = pd.DataFrame(rows, columns=COLUMNS)
    skew_rows = [{"ts": b, "caller": c, "callee": d, "skew_ms": ms} for (b, c, d), ms in skews.items()]
    return df, skew_rows


def fill_missing_segments(df: pd.DataFrame, known: pd.DataFrame | None = None) -> pd.DataFrame:
    """某個時段這個 API 有請求、但某一段一次都沒出現 → 那一段是 0 次、0 毫秒、0 錯誤（不是不知道）。
    known：另外要補的「API × 段」（例如 baseline 裡有、這次完全沒出現的段：快取全命中時 DB 那段整個消失）。"""
    if df.empty:
        return df
    segs = df[KEYS].drop_duplicates()
    if known is not None and not known.empty:
        segs = pd.concat([segs, known[KEYS]]).drop_duplicates()
    slots = df[["ts", "entry.service", "entry.name", "requests"]].drop_duplicates(["ts", "entry.service", "entry.name"])
    grid = slots.merge(segs, on=["entry.service", "entry.name"])
    out = grid.merge(df.drop(columns=["requests"]), on=["ts", *KEYS], how="left")
    out[list(METRICS)] = out[list(METRICS)].fillna(0.0)
    return out


def new_segments(cur: pd.DataFrame, base: pd.DataFrame, min_calls: float = 0.5) -> pd.DataFrame:
    """沒見過的呼叫（規則，不用學）：這支 API 有 baseline，但這一段從來沒出現過，而且每個請求至少呼叫 min_calls 次。
    例：換版後開始直連另一個資料庫、多呼叫一個外部系統。只看已經有 baseline 的 API，新上線的 API 不算。"""
    if cur.empty or base.empty:
        return cur.iloc[0:0]
    known_api = base[["entry.service", "entry.name"]].drop_duplicates()
    known_seg = base[KEYS].drop_duplicates().assign(_known=True)
    c = cur.merge(known_api, on=["entry.service", "entry.name"]).merge(known_seg, on=KEYS, how="left")
    return c[c["_known"].isna() & (c["calls"] >= min_calls)].drop(columns=["_known"])


def repeated_calls(cur: pd.DataFrame, min_calls: float) -> pd.DataFrame:
    """同一個請求裡，同一個往外的呼叫（同一句 SQL、同一個下游）平均 ≥ min_calls 次（規則，不用學）。
    N+1、迴圈裡打 API：如果從上線第一天就這樣，baseline 會把它學成「平常」，所以要有不用學的門檻。"""
    if cur.empty:
        return cur
    return cur[cur["segment"].str.contains(" → ", regex=False) & (cur["calls"] >= min_calls)]


def swallowed(cur: pd.DataFrame, min_rate: float) -> pd.DataFrame:
    """吞掉的例外（規則，不用學）：這一段丟過例外但沒標成錯誤的請求 ≥ min_rate。
    程式 catch 掉例外、回空值或 200，表面正常（假性健康）；也會學 baseline（swallowed_rate），平常就有的另外看變多沒有。"""
    if cur.empty:
        return cur
    return cur[cur["swallowed_rate"] >= min_rate]


# ── 讀寫（repo.traces 抽樣、讀 span；repo.segments 存統計）──

def collect(repo, cfg: dict, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, list[dict]]:
    """[start, end) 每個時段抽樣、算好，寫進 aiops-trace-segments（同一個時段重算會覆蓋，不會重複）。"""
    interval, ix = cfg.get("interval", "5m"), cfg["traces_index"]
    samples = [Sampled(*s) for s in repo.traces.sample_entries(ix, start, end, interval, cfg.get("sample_per_bucket", 100), cfg.get("sample_policy"))]
    if not samples:
        return pd.DataFrame(columns=COLUMNS), []
    pad = pd.Timedelta(minutes=5)
    traces = group_traces(repo.traces.spans(ix, sorted({s.trace_id for s in samples}), start - pad, end + pad))
    # 消費者用 link 指到的生產者在別的 trace 裡，另外讀回來（尾端取樣時那個 trace 可能沒被留下，就算不出排隊時間）
    want = {sid for t in traces.values() for sp in t if sp["kind"] == "Consumer" for sid in sp.get("links") or []}
    linked = repo.traces.spans_by_id(ix, sorted(want), start - pad, end + pad) if want else {}
    df, skews = summarize(samples, traces, cfg.get("clock_skew_ms", 500), linked)
    df = fill_missing_segments(df)
    repo.segments.ensure()
    docs = [(doc_id("seg", r["ts"], r["entry.service"], r["entry.name"], r["segment"]),
             {"@timestamp": r["ts"].isoformat(), "kind": "segment", "entry": {"service": r["entry.service"], "name": r["entry.name"]},
              "segment": r["segment"], "requests": int(r["requests"]), **{m: float(r[m]) for m in METRICS},
              **({"exception_type": r["exception_type"]} if isinstance(r.get("exception_type"), str) else {})})
            for r in df.to_dict("records")]
    docs += [(doc_id("skew", r["ts"], r["caller"], r["callee"]),
              {"@timestamp": r["ts"].isoformat(), "kind": "clock_skew", "caller": r["caller"], "callee": r["callee"],
               "skew_ms": r["skew_ms"]}) for r in skews]
    repo.segments.bulk(docs)
    return df, skews


def load_segments(repo, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    rows = [{"ts": pd.Timestamp(s["@timestamp"]), "entry.service": s["entry"]["service"], "entry.name": s["entry"]["name"],
             "segment": s["segment"], "requests": s["requests"], **{m: s.get(m, 0.0) for m in METRICS}}   # 舊的統計沒有 swallowed_rate
            for s in repo.segments.segments(start, end)]
    return pd.DataFrame(rows, columns=["ts", *KEYS, "requests", *METRICS])


def _metric_frame(df: pd.DataFrame, m: str) -> pd.DataFrame:
    return df[[*KEYS, "ts"]].assign(value=df[m])

def cmd_trace_merge(cfg: dict, args) -> int:
    """raw（每一段一筆）→ 一個請求一筆（traces-merged-<group>-<日期>）。排程器每分鐘跑；從上次合到的時間接著合，中斷很久會分段補（最多 2 天）。"""
    repo = Repos(ES(), load_scenario())
    at = _now(args.at).to_pydatetime()
    since, until, n = M.run(repo, cfg, at, pd.Timedelta(args.window).to_pytimedelta())
    print(f"trace-merge {since:%H:%M:%S}～{until:%H:%M:%S}：合了 {n} 個 trace")
    return 0


def cmd_trace_collect(cfg: dict, args) -> int:
    """補過去的統計（trace 原文只留 7 天，最多補 7 天），或手動重算某一段。平常由 trace-detect 每 5 分鐘順便做。"""
    repo = Repos(ES(), load_scenario())
    freq = _freq(cfg.get("interval", "5m"))
    end = _now(args.end).floor(freq)
    start = pd.Timestamp(args.start) if args.start else end - pd.Timedelta(days=7)
    step = pd.Timedelta(hours=1)
    n = 0
    t = start
    while t < end:
        df, _ = collect(repo, cfg, t, min(t + step, end))
        n += len(df)
        t += step
    print(f"統計 {start} ～ {end}：{n} 筆（API × 段 × 時段）寫進 {repo.segments.INDEX}")
    return 0

def cmd_trace_train(cfg: dict, args) -> int:
    repo = Repos(ES(), load_scenario())
    repo.baseline.ensure()
    freq = _freq(cfg.get("interval", "5m"))
    end = _now(args.end).floor(freq)
    start = end - (pd.Timedelta(args.history) if args.history else pd.Timedelta(weeks=cfg.get("history_weeks", 4)))
    topts = TrainOptions(**cfg.get("train", {}))
    hist = load_segments(repo, start, end)
    hist = hist[hist["requests"] >= cfg.get("min_requests", 5)]
    adjs = data_adjustments(repo, cfg, end)
    print(f"學習區間 {start.tz_convert(topts.timezone):%Y-%m-%d %H:%M} ～ {end.tz_convert(topts.timezone):%Y-%m-%d %H:%M}"
          f"（{topts.timezone}），{len(hist)} 筆統計")
    for m in METRICS:
        h, dropped = A.train_filter(_metric_frame(hist, m), KEYS, f"trace.segment.{m}", adjs, topts.timezone)
        b = train(h, KEYS, topts)
        save_baseline(repo, f"trace.segment.{m}", KEYS, b, start, end)
        print(train_summary(f"trace.segment.{m}", KEYS, h, b) + (f"；排除 {dropped} 個點" if dropped else ""))
    return 0

def cmd_trace_detect(cfg: dict, args) -> int:
    """算最近一段時間每個 API 每一段的統計（同時寫進 aiops-trace-segments，之後 train 用），跟 baseline 比。"""
    repo = Repos(ES(), load_scenario())
    repo.anomalies.ensure()
    freq = _freq(cfg.get("interval", "5m"))
    end = _now(args.at).floor(freq)
    start = end - pd.Timedelta(args.window)
    tz = cfg.get("train", {}).get("timezone", "Asia/Taipei")
    dopts = DetectOptions(**cfg.get("detect", {}))
    print(f"偵測區間 {start.tz_convert(tz):%Y-%m-%d %H:%M} ～ {end.tz_convert(tz):%Y-%m-%d %H:%M}（{tz}）")
    cur, skews = collect(repo, cfg, start, end)
    adjs = data_adjustments(repo, cfg, end)
    cur = cur[cur["requests"] >= cfg.get("min_requests", 5)]
    total = 0
    for m in METRICS:
        name = f"trace.segment.{m}"
        base = repo.baseline.load(name, KEYS)
        if cur.empty or base.empty:
            print(f"  {name}：{'沒有資料' if cur.empty else '沒有 baseline，先跑 trace-train'}")
            continue
        # baseline 裡有、這次完全沒出現的段補 0（例如平常會查 DB 的那段整個消失）
        c = fill_missing_segments(cur, base[KEYS].drop_duplicates())
        rule = cfg.get("metrics", {}).get(m, {})
        s = score(_metric_frame(c, m), base, KEYS, direction=rule.get("direction", "both"),
                  min_abs=rule.get("min_abs", 0.0), opts=dopts, tz=tz)
        s, _ = A.detect_filter(s, KEYS, name, adjs, tz, baseline=base)
        s = with_status(cfg, s, KEYS, cfg.get("interval", "5m"), start)
        total += save_anomalies(repo, name, KEYS, s, rule.get("direction", "both"), tz)
    # 沒見過的呼叫：規則，這支 API 有 baseline、但這一段從來沒出現過
    base = repo.baseline.load("trace.segment.calls", KEYS)
    new = new_segments(cur, base)
    now = pd.Timestamp.now(tz="UTC").isoformat()
    found = [Anomaly(ts=r["ts"], series="trace.segment.new", keys={k: r[k] for k in KEYS}, value=r["calls"],
                     median=0.0, expected_low=0.0, expected_high=0.0, level="rule", direction="up")
             for r in new.to_dict("records")]
    docs = [(a.id(), a.to_doc(now)) for a in found]
    if docs:
        repo.anomalies.write(docs)
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
                  for r in repeated_calls(cur, rules["repeated_calls"]).to_dict("records")]
    if rules.get("swallowed_rate"):
        found += [("trace.segment.swallowed", r, r["swallowed_rate"], rules["swallowed_rate"],
                   f"吞掉的例外：{r['entry.service']} {r['entry.name']} → {r['segment']}"
                   f"（{r['swallowed_rate']:.0%} 的請求丟了 {r.get('exception_type') or '例外'}，但沒標成錯誤）")
                  for r in swallowed(cur, rules["swallowed_rate"]).to_dict("records")]
    docs = [(a.id(), a.to_doc(now)) for a in (
        Anomaly(ts=r["ts"], series=series, rule=series, keys={k: r[k] for k in KEYS}, value=v, expected_high=th,
                level="rule", direction="up", severity="warning", message=msg) for series, r, v, th, msg in found)]
    if docs:
        repo.anomalies.write(docs)
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
        repo.anomalies.write(docs)
        for _, d in docs[:5]:
            print(f"  時鐘不同步：{d['keys']['caller']} → {d['keys']['callee']} 差 {d['value']:.0f} ms")
    total += len(docs)
    print(f"共 {total} 個異常")
    return 0

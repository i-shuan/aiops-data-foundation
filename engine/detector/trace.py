"""trace 每一段的 baseline：每個 API 的每個請求，平常呼叫每一段幾次、每段花多久、出錯的比例。

為什麼要有（docs/rca/cases.md）：N+1（每個請求打同一句 SQL 100 次）、重試風暴（一個請求打下游 4 次）、
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

from core.es import ES, doc_id, time_range
from core.traces import group_traces, segment, self_times, spans_by_id, spans_of

SEGMENT_INDEX = "aiops-trace-segments"
SEGMENT_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "kind": {"type": "keyword"},
    "entry": {"properties": {"service": {"type": "keyword"}, "name": {"type": "keyword"}}},
    "segment": {"type": "keyword"}, "requests": {"type": "integer"},
    "calls": {"type": "double"}, "time_ms": {"type": "double"}, "error_rate": {"type": "double"},
    "swallowed_rate": {"type": "double"}, "exception_type": {"type": "keyword"},
    "caller": {"type": "keyword"}, "callee": {"type": "keyword"}, "skew_ms": {"type": "double"},
}}}
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
    吞掉的例外：這一段丟過例外（exception），但 status 不是錯誤（程式 catch 掉了，例如回空值、回 200）。"""
    st = self_times(trace)
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
        if s.get("exception") and not s["error"]:
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


# ── 讀寫 ES ──

def sample_entries(es: ES, index: str, start: pd.Timestamp, end: pd.Timestamp, interval: str, per_bucket: int,
                   policy: str | None = None) -> list[Sampled]:
    """每個時段、每個入口（服務的 Server span、訊息消費者的 Consumer span、沒有上一層的 span）抽 per_bucket 個請求。依 trace_id 排序取前幾個：trace_id 是隨機的，等於隨機抽。
    policy：尾端取樣時只抽這條規則留下的（random：隨機那一份）。錯的、慢的全留，混進來平均會偏高；
    沒有標記的（還沒經過尾端取樣的舊資料）照樣抽。"""
    sources = [{"svc": {"terms": {"field": "resource.attributes.service.name"}}},
               {"name": {"terms": {"field": "name"}}},
               {"ts": {"date_histogram": {"field": "@timestamp", "fixed_interval": interval}}}]
    # 入口：HTTP 請求（Server）、訊息消費者（Consumer），以及「沒有上一層的 span」：
    # .NET 自動埋點的 Kafka 消費者處理訊息時查 DB，那個 DB 的 span 沒有上一層、自成一個 trace（串不回訊息），
    # 也當成入口算，至少看得到「這個服務查 DB 變慢、出錯」（docs/trace/scenarios.md 場景四）
    entry = {"bool": {"should": [{"terms": {"kind": ["Server", "Consumer"]}},
                                 {"bool": {"must_not": {"exists": {"field": "parent_span_id"}}}}], "minimum_should_match": 1}}
    filters = [time_range(start, end), entry]
    if policy:
        f = "scope.attributes.tailsampling.policy"
        filters.append({"bool": {"should": [{"term": {f: policy}}, {"bool": {"must_not": {"exists": {"field": f}}}}]}})
    body = {"size": 0, "query": {"bool": {"filter": filters}},
            "aggs": {"e": {"composite": {"size": 1000, "sources": sources},
                           "aggs": {"t": {"top_hits": {"size": per_bucket, "sort": [{"trace_id": "asc"}],
                                                       "_source": ["trace_id", "span_id"]}}}}}}
    out, after = [], None
    while True:
        if after:
            body["aggs"]["e"]["composite"]["after"] = after
        agg = es.search(index, body).get("aggregations", {}).get("e", {})
        for b in agg.get("buckets", []):
            ts = pd.Timestamp(b["key"]["ts"], unit="ms", tz="UTC")
            out += [Sampled(ts, h["_source"]["trace_id"], h["_source"]["span_id"]) for h in b["t"]["hits"]["hits"]]
        after = agg.get("after_key")
        if not after:
            break
    return out


def collect(es: ES, cfg: dict, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, list[dict]]:
    """[start, end) 每個時段抽樣、算好，寫進 aiops-trace-segments（同一個時段重算會覆蓋，不會重複）。"""
    interval = cfg.get("interval", "5m")
    samples = sample_entries(es, cfg["traces_index"], start, end, interval, cfg.get("sample_per_bucket", 20),
                             cfg.get("sample_policy"))
    if not samples:
        return pd.DataFrame(columns=COLUMNS), []
    pad = pd.Timedelta(minutes=5)
    traces = group_traces(spans_of(es, cfg["traces_index"], sorted({s.trace_id for s in samples}), start - pad, end + pad))
    # 消費者用 link 指到的生產者在別的 trace 裡，另外讀回來（尾端取樣時那個 trace 可能沒被留下，就算不出排隊時間）
    want = {sid for t in traces.values() for sp in t if sp["kind"] == "Consumer" for sid in sp.get("links") or []}
    linked = spans_by_id(es, cfg["traces_index"], sorted(want), start - pad, end + pad) if want else {}
    df, skews = summarize(samples, traces, cfg.get("clock_skew_ms", 500), linked)
    df = fill_missing_segments(df)
    es.ensure_index(SEGMENT_INDEX, SEGMENT_MAPPING)
    docs = [(doc_id("seg", r["ts"], r["entry.service"], r["entry.name"], r["segment"]),
             {"@timestamp": r["ts"].isoformat(), "kind": "segment", "entry": {"service": r["entry.service"], "name": r["entry.name"]},
              "segment": r["segment"], "requests": int(r["requests"]), **{m: float(r[m]) for m in METRICS},
              **({"exception_type": r["exception_type"]} if isinstance(r.get("exception_type"), str) else {})})
            for r in df.to_dict("records")]
    docs += [(doc_id("skew", r["ts"], r["caller"], r["callee"]),
              {"@timestamp": r["ts"].isoformat(), "kind": "clock_skew", "caller": r["caller"], "callee": r["callee"],
               "skew_ms": r["skew_ms"]}) for r in skews]
    if docs:
        es.bulk_index(SEGMENT_INDEX, docs)
    return df, skews


def load_segments(es: ES, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    rows, after = [], None
    while True:
        body = {"size": 10000, "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}],
                "query": {"bool": {"filter": [time_range(start, end), {"term": {"kind": "segment"}}]}}}
        if after:
            body["search_after"] = after
        hits = es.search(SEGMENT_INDEX, body)["hits"]["hits"]
        for h in hits:
            s = h["_source"]
            rows.append({"ts": pd.Timestamp(s["@timestamp"]), "entry.service": s["entry"]["service"],
                         "entry.name": s["entry"]["name"], "segment": s["segment"], "requests": s["requests"],
                         **{m: s.get(m, 0.0) for m in METRICS}})   # 舊的統計沒有 swallowed_rate
        if len(hits) < 10000:
            break
        after = hits[-1]["sort"]
    return pd.DataFrame(rows, columns=["ts", *KEYS, "requests", *METRICS])

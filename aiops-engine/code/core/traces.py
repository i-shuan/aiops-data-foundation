"""trace 的共用處理：從 ES 讀 span、組回一個個請求、算每一段自己花的時間。根因分析與 trace 的 baseline 都用。

span 的格式（to_span 從 raw 的 ES 文件轉成這樣；from_merged 從合成一筆的文件展開成同樣的格式）：
  {"trace_id", "span_id", "parent", "service", "name", "kind", "target", "start_ms", "dur_ms", "error", "exception", "attrs", "resource"}
  exception：這一段丟過的例外種類（Collector 從例外事件抄到 exception.type 屬性），沒有就是 None
  links：這一段連到的其他 span（span_id）。.NET 自動埋點的 Kafka 消費者（receive）是新的 trace，只用 link 指回生產者
  target：往外呼叫的對象（peer.service、db.system、db.system.name、server.address），沒有就是 None

兩種 index：
  raw    traces-generic.otel-<group>：Collector 寫的、每一段一筆、OTel 原生格式，只留 2 天（trace baseline、資料品質讀這份）
  merged traces-merged-<group>-<日期>：detector trace-merge 合成的、一個請求一筆（根因分析讀這份，事故可能幾天後才查）
  spans_of、spans_by_id 看 index 名稱決定怎麼讀；回傳的 span 格式一樣，後面的函式不用管來源。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .es import ES, get_field, time_range

_SPAN_FIELDS = ["@timestamp", "trace_id", "span_id", "parent_span_id", "name", "kind", "duration", "status.code", "links",
                "resource.attributes", "attributes"]


def is_merged(index: str) -> bool:
    return "traces-merged" in index


def _duration_ms(v) -> float:
    """奈秒 → 毫秒。壞 SDK 送來的格式錯誤值（字串、負數）當 0，不能讓一筆壞 span 擋住整批合併或判斷。"""
    try:
        d = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return d / 1e6 if d > 0 else 0.0


def to_span(src: dict) -> dict:
    attrs = src.get("attributes") or {}
    # db.system.name：新版 DB 語意（舊的 db.system 改名）；沒有對象的 Client span 段名沒有「→」，N+1 規則看不到
    target = (get_field(attrs, "peer.service") or get_field(attrs, "db.system") or get_field(attrs, "db.system.name")
              or get_field(attrs, "server.address"))
    ts = src["@timestamp"]
    # ES 回的 @timestamp 可能是數字、數字的字串（奈秒精度時，例如 "1791687912264.916000"），或 ISO 格式
    if isinstance(ts, str) and ts.replace(".", "", 1).isdigit():
        ts = float(ts)
    start_ms = float(ts) if isinstance(ts, (int, float)) else datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
    return {"trace_id": src["trace_id"], "span_id": src["span_id"], "parent": src.get("parent_span_id") or None,
            "service": get_field(src, "resource.attributes.service.name") or "?", "name": src.get("name", "?"),
            "kind": src.get("kind", ""), "target": target, "start_ms": start_ms,
            "dur_ms": _duration_ms(src.get("duration")), "error": get_field(src, "status.code") == "Error",
            "exception": get_field(attrs, "exception.type"),
            "links": [l["span_id"] for l in src.get("links") or [] if l.get("span_id")],
            "attrs": attrs, "resource": get_field(src, "resource.attributes") or {}}


def from_merged(doc: dict) -> list[dict]:
    """合成一筆的文件 → 一個個 span（跟 to_span 同一種格式）。"""
    return [{"trace_id": doc["trace_id"], "span_id": s["span_id"], "parent": s.get("parent") or None, "service": s.get("service", "?"),
             "name": s.get("name", "?"), "kind": s.get("kind", ""), "target": s.get("target"), "start_ms": float(s["start_ms"]),
             "dur_ms": float(s.get("dur_ms") or 0), "error": bool(s.get("error")), "exception": s.get("exception"),
             "links": list(s.get("links") or []), "attrs": s.get("attrs") or {}, "resource": s.get("resource") or {}}
            for s in doc.get("spans") or []]


def spans_by_id(es: ES, index: str, span_ids: list[str], start: datetime, end: datetime) -> dict[str, dict]:
    """用 span_id 讀回 span（例如 link 指到的、另一個 trace 裡的生產者）。"""
    out = {}
    want = set(span_ids)
    for i in range(0, len(span_ids), 1000):
        chunk = span_ids[i:i + 1000]
        if is_merged(index):
            body = {"size": 1000, "query": {"bool": {"filter": [time_range(start, end),
                                                                 {"nested": {"path": "spans", "query": {"terms": {"spans.span_id": chunk}}}}]}}}
            for h in es.search(index, body)["hits"]["hits"]:
                out.update({s["span_id"]: s for s in from_merged(h["_source"]) if s["span_id"] in want})
        else:
            body = {"size": 1000, "_source": _SPAN_FIELDS,
                    "query": {"bool": {"filter": [time_range(start, end), {"terms": {"span_id": chunk}}]}}}
            out.update({h["_source"]["span_id"]: to_span(h["_source"]) for h in es.search(index, body)["hits"]["hits"]})
    return out


def spans_of(es: ES, index: str, trace_ids: list[str], start: datetime, end: datetime) -> list[dict]:
    """把這些 trace 的所有 span 讀回來（含其他服務的）。時間放寬，跨過邊界的請求也讀得完整。"""
    out = []
    for i in range(0, len(trace_ids), 500):
        ids = trace_ids[i:i + 500]
        if is_merged(index):
            body = {"size": 500, "query": {"bool": {"filter": [time_range(start, end), {"terms": {"trace_id": ids}}]}}}
            for h in es.search(index, body)["hits"]["hits"]:
                out += from_merged(h["_source"])
            continue
        after = None
        while True:
            body = {"size": 5000, "_source": _SPAN_FIELDS, "sort": [{"@timestamp": "asc"}, {"span_id": "asc"}],
                    "query": {"bool": {"filter": [time_range(start, end), {"terms": {"trace_id": ids}}]}}}
            if after:
                body["search_after"] = after
            hits = es.search(index, body)["hits"]["hits"]
            out += [to_span(h["_source"]) for h in hits]
            if len(hits) < 5000:
                break
            after = hits[-1]["sort"]
    return out


def segment(s: dict) -> str:
    """一段的名稱：服務 › 做什麼（→ 呼叫誰）。比較、排名都以這個為單位。"""
    label = f"{s['service']} › {s['name']}"
    return f"{label} → {s['target']}" if s.get("target") and s["kind"] == "Client" else label


def group_traces(spans: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        out[s["trace_id"]].append(s)
    return out


def self_times(trace: list[dict]) -> dict[str, float]:
    """每個 span 自己花的時間（毫秒）＝ 它的耗時 − 子 span 佔掉的時間（重疊的只算一次）。

    例：wip-worker 處理 3 秒，其中 2.9 秒在等 Oracle → wip-worker 自己 0.1 秒、等 Oracle 那段 2.9 秒。
    這樣慢的那一段才會被算在真正慢的地方，不會一路算到最上層的服務。
    """
    children: dict[str, list[dict]] = defaultdict(list)
    for s in trace:
        if s.get("parent"):
            children[s["parent"]].append(s)
    out = {}
    for s in trace:
        lo, hi = s["start_ms"], s["start_ms"] + s["dur_ms"]
        iv = sorted((max(lo, c["start_ms"]), min(hi, c["start_ms"] + c["dur_ms"])) for c in children.get(s["span_id"], []))
        covered, cur_lo, cur_hi = 0.0, None, None
        for a, b in iv:
            if b <= a:
                continue
            if cur_hi is None or a > cur_hi:
                if cur_hi is not None:
                    covered += cur_hi - cur_lo
                cur_lo, cur_hi = a, b
            else:
                cur_hi = max(cur_hi, b)
        if cur_hi is not None:
            covered += cur_hi - cur_lo
        out[s["span_id"]] = max(0.0, s["dur_ms"] - covered)
    return out


def error_origins(trace: list[dict]) -> list[dict]:
    """錯誤最早出在哪：標了錯誤、但底下的子 span 都沒錯的那些。上層只是把錯誤往上傳，不是源頭。"""
    has_err_child = {s["parent"] for s in trace if s["error"] and s.get("parent")}
    return [s for s in trace if s["error"] and s["span_id"] not in has_err_child]


def root_of(trace: list[dict]) -> dict:
    ids = {s["span_id"] for s in trace}
    roots = [s for s in trace if not s.get("parent") or s["parent"] not in ids]
    return min(roots, key=lambda s: s["start_ms"]) if roots else trace[0]

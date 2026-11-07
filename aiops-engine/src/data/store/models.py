"""trace 的資料物件與共用處理（②清資料：raw 的 span → 統一的 span → 一個請求一筆）：

從 ES 讀回的 raw 文件（OTel 或 Elastic APM）轉成統一的 span 格式、組回一個個請求、算每一段自己花的時間、合成 traces-merged-* 的文件。
根因分析與 trace 的 baseline 都用這份；異常（Anomaly）與基準線的資料物件在 detect/store/models.py。
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from data.source import DEFAULT as DEFAULT_SOURCE, for_index


# ── trace ──
MERGED_LIKE = ("traces-merged", "lot-paths")   # engine 合成、一個請求（一個 lot）一筆的索引；lot-paths 是工廠的履歷（plugins/factory/paths.py）


def schema_of(index: str, source: str = DEFAULT_SOURCE) -> str:
    """merged（engine 合成的：traces-merged-*、lot-paths-*），或 raw 的來源名（apm、otel；由 data/source 決定）。
    source 是場景宣告的原始資料格式（Scenario.source）。"""
    if any(m in index for m in MERGED_LIKE):
        return "merged"
    return for_index(index, source).name


def is_merged(index: str) -> bool:
    return any(m in index for m in MERGED_LIKE)


def trace_id_field(index: str, source: str = DEFAULT_SOURCE) -> str:
    return for_index(index, source).trace_id_field


def from_merged(doc: dict) -> list[dict]:
    """合成一筆的文件 → 一個個 span（跟 to_span 同一種格式）。"""
    return [{"trace_id": doc["trace_id"], "span_id": s["span_id"], "parent": s.get("parent") or None, "service": s.get("service", "?"),
             "name": s.get("name", "?"), "kind": s.get("kind", ""), "target": s.get("target"), "start_ms": float(s["start_ms"]),
             "dur_ms": float(s.get("dur_ms") or 0), "error": bool(s.get("error")), "exception": s.get("exception"),
             "links": list(s.get("links") or []), "attrs": s.get("attrs") or {}, "resource": s.get("resource") or {}}
            for s in doc.get("spans") or []]


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


# ── 服務之間誰呼叫誰（根因分析的拓樸、IT 併件的依據）──

@dataclass
class Edge:
    caller: str
    callee: str
    calls: int = 0
    errors: int = 0
    durations: list[float] = field(default_factory=list)

    def p95(self) -> float:
        d = sorted(self.durations)
        return d[min(len(d) - 1, int(0.95 * len(d)))] if d else 0.0


def topology(traces: dict[str, list[dict]]) -> list[Edge]:
    """從 trace 畫出服務之間誰呼叫誰：
    - 子 span 的服務跟父 span 不同 → 父的服務呼叫子的服務（例如 mes-api → wip-worker）
    - 往外呼叫、底下沒有別的服務接（資料庫、外部 API）→ 服務呼叫那個對象（例如 wip-worker → oracle）
    """
    edges: dict[tuple[str, str], Edge] = {}

    def add(a, b, s):
        e = edges.setdefault((a, b), Edge(a, b))
        e.calls += 1
        e.errors += bool(s["error"])
        e.durations.append(s["dur_ms"])

    for t in traces.values():
        by_id = {s["span_id"]: s for s in t}
        has_child = {s["parent"] for s in t if s.get("parent")}
        for s in t:
            p = by_id.get(s.get("parent"))
            if p and p["service"] != s["service"]:
                add(p["service"], s["service"], s)
            elif s["kind"] == "Client" and s.get("target") and s["span_id"] not in has_child:
                add(s["service"], s["target"], s)
    return sorted(edges.values(), key=lambda e: (e.caller, e.callee))


def mermaid(edges: list[Edge]) -> str:
    """給 Markdown 用的關係圖（GitHub、Kibana 的 Markdown 都看得懂 mermaid）。"""
    def node(n):
        return re.sub(r"[^A-Za-z0-9_]", "_", n)
    lines = ["graph LR"]
    for e in edges:
        err = f"，錯誤 {e.errors}" if e.errors else ""
        lines.append(f'  {node(e.caller)}["{e.caller}"] -->|"{e.calls} 次，p95 {e.p95():.0f} ms{err}"| {node(e.callee)}["{e.callee}"]')
    return "\n".join(lines)


# ── 一個 trace 合成一筆（traces-merged-*、工廠的 lot-paths-*）──

MERGED_PREFIX = "traces-merged"


def is_entry(s: dict, ids: set[str]) -> bool:
    """入口：HTTP 請求（Server）、訊息消費者（Consumer）、沒有上一層的 span（.NET 自動埋點的 Kafka 消費者查 DB 那段自成一個 trace）。"""
    return s["kind"] in ("Server", "Consumer") or not s.get("parent") or s["parent"] not in ids


def merged_doc(spans: list[dict], merged_at: datetime, prefix: str = MERGED_PREFIX) -> tuple[str, dict]:
    """同一個 trace 的 span → (index 名稱, 文件)。index 依 group 與根 span 的日期（UTC）分。
    文件：上層是摘要（入口、總耗時、有沒有錯、經過哪些服務），spans 是每一段（nested），entries 是入口，讓「依服務／API 挑請求」不用展開 spans。
    prefix：index 名的前綴；工廠的履歷用 lot-paths（detect/trace/path_builder.py），文件格式一樣。"""
    root = root_of(spans)
    ids = {s["span_id"] for s in spans}
    start = datetime.fromtimestamp(root["start_ms"] / 1000, timezone.utc)
    end_ms = max(s["start_ms"] + s["dur_ms"] for s in spans)
    res = root.get("resource") or {}
    group = res.get("group") or res.get("data_stream.namespace") or "unassigned"
    doc = {
        "@timestamp": start.isoformat(), "trace_id": root["trace_id"], "group": group, "dept": res.get("dept"),
        "root": {"service": root["service"], "name": root["name"], "kind": root["kind"], "error": bool(root["error"])},
        "duration_ms": round(max(root["dur_ms"], end_ms - root["start_ms"]), 3),
        "error": any(s["error"] for s in spans),
        "services": sorted({s["service"] for s in spans}),
        "span_count": len(spans),
        "merged_at": merged_at.isoformat(),
        "entries": [{"span_id": s["span_id"], "service": s["service"], "name": s["name"], "kind": s["kind"],
                     "start_ms": s["start_ms"], "duration_ms": s["dur_ms"], "error": bool(s["error"])}
                    for s in spans if is_entry(s, ids)],
        "spans": [{"span_id": s["span_id"], "parent": s.get("parent"), "service": s["service"], "name": s["name"], "kind": s["kind"],
                   "target": s.get("target"), "start_ms": s["start_ms"], "dur_ms": s["dur_ms"], "error": bool(s["error"]),
                   "exception": s.get("exception"), "links": s.get("links") or [], "attrs": s.get("attrs") or {},
                   "resource": s.get("resource") or {}} for s in sorted(spans, key=lambda x: (x["start_ms"], x["span_id"]))],
    }
    return f"{prefix}-{group}-{start:%Y.%m.%d}", doc


class Trace:
    """一個 trace（同 trace_id 的 span，core 的 span 格式）。span 本身還是 dict：讀回來的量大、各處都是 dict 的寫法；
    這個類別把「一個 trace 怎麼算」收在一起：根 span、每段自己花的時間、錯誤源頭、合成一筆、好幾個 trace 的拓樸。"""

    def __init__(self, spans: list[dict]):
        self.spans = spans

    @classmethod
    def group(cls, spans: list[dict]) -> dict[str, "Trace"]:
        return {tid: cls(ss) for tid, ss in group_traces(spans).items()}

    @property
    def trace_id(self) -> str:
        return self.spans[0]["trace_id"]

    def root(self) -> dict:
        return root_of(self.spans)

    def self_times(self) -> dict[str, float]:
        return self_times(self.spans)

    def error_origins(self) -> list[dict]:
        return error_origins(self.spans)

    def to_merged_doc(self, merged_at: datetime, prefix: str = MERGED_PREFIX) -> tuple[str, dict]:
        return merged_doc(self.spans, merged_at, prefix)

    @staticmethod
    def edges(traces) -> list[Edge]:
        """好幾個 trace（Trace 或 span 清單）之間服務誰呼叫誰。"""
        return topology({k: (t.spans if isinstance(t, Trace) else t) for k, t in dict(traces).items()})

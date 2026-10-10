"""根因分析的計算，不碰 ES：把 span 組回一個個請求、算每一段自己花的時間、比較出事時和平常、
畫瀑布圖、畫服務之間的呼叫關係、把 log 歸成樣式。

span 的格式（es.py 從 ES 讀出來後轉成這樣）：
  {"trace_id", "span_id", "parent", "service", "name", "kind", "target", "start_ms", "dur_ms", "error"}
  target：往外呼叫的對象（peer.service、db.system、server.address），沒有就是 None
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field


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


@dataclass
class SegmentDiff:
    segment: str
    now_ms: float          # 出事時：每個請求在這一段平均花多久（自己的時間）
    ref_ms: float          # 平常
    now_calls: float       # 每個請求呼叫這一段幾次
    ref_calls: float
    now_errors: int = 0    # 錯誤源頭在這一段的請求數
    ref_errors: int = 0

    @property
    def delta_ms(self) -> float:
        return self.now_ms - self.ref_ms


def _per_segment(traces: dict[str, list[dict]]):
    total, calls, errs = defaultdict(float), defaultdict(int), defaultdict(int)
    for t in traces.values():
        st = self_times(t)
        for s in t:
            seg = segment(s)
            total[seg] += st[s["span_id"]]
            calls[seg] += 1
        for seg in {segment(s) for s in error_origins(t)}:
            errs[seg] += 1
    n = max(len(traces), 1)
    return {k: v / n for k, v in total.items()}, {k: v / n for k, v in calls.items()}, errs


def compare_segments(now: dict[str, list[dict]], ref: dict[str, list[dict]]) -> list[SegmentDiff]:
    """出事時和平常比，每一段每個請求多花了多久。排在最前面的就是最可能的瓶頸。"""
    nt, nc, ne = _per_segment(now)
    rt, rc, re_ = _per_segment(ref)
    out = [SegmentDiff(seg, nt.get(seg, 0.0), rt.get(seg, 0.0), nc.get(seg, 0.0), rc.get(seg, 0.0), ne.get(seg, 0), re_.get(seg, 0))
           for seg in set(nt) | set(rt)]
    return sorted(out, key=lambda d: d.delta_ms, reverse=True)


def root_of(trace: list[dict]) -> dict:
    ids = {s["span_id"] for s in trace}
    roots = [s for s in trace if not s.get("parent") or s["parent"] not in ids]
    return min(roots, key=lambda s: s["start_ms"]) if roots else trace[0]


def waterfall(trace: list[dict], width: int = 40) -> list[str]:
    """瀑布圖（文字版）：每一段一行，依開始時間排，縮排表示誰呼叫誰，長條是在整個請求裡的位置和長度。"""
    root = root_of(trace)
    t0, total = root["start_ms"], max(root["dur_ms"], 1e-9)
    by_parent: dict[str | None, list[dict]] = defaultdict(list)
    for s in trace:
        if s is not root:
            by_parent[s.get("parent")].append(s)
    st = self_times(trace)
    lines = []

    def walk(s, depth):
        a = int(round((s["start_ms"] - t0) / total * width))
        b = max(a + 1, int(round((s["start_ms"] + s["dur_ms"] - t0) / total * width)))
        a, b = max(0, min(a, width - 1)), max(1, min(b, width))
        bar = " " * a + "█" * (b - a) + " " * (width - b)
        mark = " ✗" if s["error"] else ""
        lines.append(f"{'  ' * depth}{segment(s)}".ljust(60) + f" |{bar}| {s['dur_ms']:>8.1f} ms（自己 {st[s['span_id']]:.1f}）{mark}")
        for c in sorted(by_parent.get(s["span_id"], []), key=lambda c: c["start_ms"]):
            walk(c, depth + 1)

    walk(root, 0)
    return lines


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


_PATTERNS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<id>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
]


def log_pattern(message: str) -> str:
    """把一行 log 變成樣式：數字、IP、ID 換成記號。同一種錯誤只差在批號、時間、IP 的，會變成同一個樣式。"""
    m = (message or "").strip().splitlines()[0] if message else ""
    for rx, rep in _PATTERNS:
        m = rx.sub(rep, m)
    return m[:200]


@dataclass
class PatternDiff:
    service: str
    pattern: str
    now: int
    ref: int
    example: str

    @property
    def is_new(self) -> bool:
        return self.ref == 0


def compare_patterns(now: list[dict], ref: list[dict]) -> list[PatternDiff]:
    """出事時的 log 樣式跟平常比：新出現的排最前面，其次是變多最多的。
    log 的格式：{"service", "message"}；now、ref 要是一樣長的時間。"""
    def count(logs):
        c, ex = defaultdict(int), {}
        for lg in logs:
            k = (lg["service"], log_pattern(lg["message"]))
            c[k] += 1
            ex.setdefault(k, lg["message"])
        return c, ex
    nc, ex = count(now)
    rc, _ = count(ref)
    out = [PatternDiff(k[0], k[1], n, rc.get(k, 0), ex[k]) for k, n in nc.items() if n > rc.get(k, 0)]
    return sorted(out, key=lambda p: (not p.is_new, -(p.now - p.ref)))

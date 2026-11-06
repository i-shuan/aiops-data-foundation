"""根因分析的計算，不碰 ES：比較出事時和平常、畫瀑布圖、畫服務之間的呼叫關係、把 log 歸成樣式。
span 的格式與「每一段自己的時間」見 core.traces。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median

from core.connection import get_field
from core.entity.trace import error_origins, root_of, segment, self_times


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


@dataclass
class Common:
    dim: str           # 看什麼：k8s.pod.name、service.version、lot.id、db.statement…
    value: str         # 哪個值（resource 的值前面帶服務名：wip-worker/wip-worker-7d9f-abcde）
    now: int           # 慢的請求裡幾個有這個值
    now_share: float   # 占慢的請求幾成
    ref_share: float   # 平常那批占幾成

    @property
    def lift(self) -> float:
        return self.now_share / self.ref_share if self.ref_share else float("inf")

    @property
    def is_new(self) -> bool:
        """平常那批完全沒有這個值：換版後的新 Pod、新版本（不是「這個 Pod 比別的慢」，是「平常沒見過它」）。"""
        return self.ref_share == 0


def _dims_of(trace: list[dict], res_keys, attr_keys) -> set[tuple[str, str]]:
    """一個請求帶的 (看什麼, 值)：resource（Pod、版本、節點）每個 span 的都看、值帶服務名；attributes（業務 ID、SQL）看每個 span。"""
    out = set()
    for s in trace:
        res, attrs = s.get("resource") or {}, s.get("attrs") or {}
        for k in res_keys:
            v = get_field(res, k)
            if v not in (None, ""):
                out.add((k, f"{s['service']}/{v}"))
        for k in attr_keys:
            v = get_field(attrs, k)
            if v not in (None, ""):
                out.add((k, str(v)[:120]))
    return out


def commonality(now: dict[str, list[dict]], ref: dict[str, list[dict]], *, res_keys=("k8s.pod.name", "service.version", "k8s.node.name"),
                attr_keys=("lot.id", "db.statement"), min_share: float = 0.6, min_lift: float = 2.0, min_traces: int = 5) -> list[Common]:
    """慢的請求有什麼共同點：出事時抽的那批（最慢的）vs 平常那批，哪個值在慢的裡面占 ≥ min_share、而且是平常的 ≥ min_lift 倍。
    只有 1 個 Pod 的服務，Pod 這一項兩邊都 100%，不會被列；沒有平常那批就不算（分不出是集中還是本來就只有一個值）。"""
    if len(now) < min_traces or not ref:
        return []
    cn, cr = Counter(), Counter()
    for t in now.values():
        cn.update(_dims_of(t, res_keys, attr_keys))
    for t in ref.values():
        cr.update(_dims_of(t, res_keys, attr_keys))
    out = []
    for (dim, val), n in cn.items():
        share, rshare = n / len(now), cr.get((dim, val), 0) / len(ref)
        if share >= min_share and share >= min_lift * rshare:
            out.append(Common(dim, val, n, share, rshare))
    return sorted(out, key=lambda c: (-min(c.lift, 1e9), -c.now_share))


def classify_trend(p95s: list[float], now_p95: float) -> dict:
    """出事前幾段的 p95（由舊到新）→ 突然還是慢慢惡化：
      gradual：一路往上（≥ 70% 的步是漲的）、最後 ≥ 最前 1.5 倍
      sudden ：出事前都在中位數 ±30% 內、出事時 ≥ 1.5 倍
      mixed  ：其他（起伏）；unknown：資料不到 3 段"""
    vals = [float(v) for v in p95s if v]
    out = {"p95_ms": vals, "now_p95_ms": float(now_p95 or 0), "kind": "unknown"}
    if len(vals) < 3:
        return out
    first, last = median(vals[:2]), median(vals[-2:])
    steps = list(zip(vals, vals[1:]))
    ups = sum(b > a for a, b in steps)
    base = median(vals)
    if last >= 1.5 * first and ups >= 0.7 * len(steps):
        out["kind"] = "gradual"
    elif max(vals) <= 1.3 * base and min(vals) >= 0.7 * base and out["now_p95_ms"] >= 1.5 * base:
        out["kind"] = "sudden"
    else:
        out["kind"] = "mixed"
    return out


def describe_trend(trend: dict, span_label: str, fmt) -> str:
    """給報告、結論用的一句話。span_label：例如「1 小時」。"""
    vals = trend.get("p95_ms") or []
    seq = "、".join(fmt(v) for v in vals)
    k = trend.get("kind")
    if k == "gradual":
        return f"出事前 {span_label}的 p95 一路往上（{seq}）→ **慢慢惡化**：像容量不夠、洩漏、資料量長大，不是一下壞的"
    if k == "sudden":
        return f"出事前 {span_label}的 p95 都在 {fmt(median(vals))} 左右（{seq}），出事時 {fmt(trend['now_p95_ms'])} → **突然**：像換版、改設定、依賴掛掉"
    if k == "mixed":
        return f"出事前 {span_label}的 p95 起伏（{seq}），看不出是突然還是慢慢惡化"
    return f"出事前 {span_label}的 trace 不夠（{seq or '沒有'}），看不出趨勢"

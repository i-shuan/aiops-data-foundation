"""detect 的資料物件與 mapping：

- anomaly：④⑤ aiops-anomalies 的契約：index 名、mapping、Anomaly（寫進去的一筆）。
- baseline：動態基準線（aiops-baseline）的索引與 mapping：一條序列、一個對象、每個星期幾 × 小時一筆（median、mad、p05、p95、n）。
- trace：trace 的共用處理：從 ES 讀 span、組回一個個請求、算每一段自己花的時間。根因分析與 trace 的 baseline 都用。
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from shared.infra.es import doc_id, get_field


# ── anomaly ──
ANOMALY_INDEX = "aiops-anomalies"
KEYS_MAPPING = {"type": "flattened"}     # 序列的 keys（對象）：欄位隨序列設定而變，用 flattened
ANOMALY_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "series": {"type": "keyword"}, "keys": KEYS_MAPPING,
    "value": {"type": "double"}, "median": {"type": "double"}, "expected_low": {"type": "double"},
    "expected_high": {"type": "double"}, "z": {"type": "double"}, "level": {"type": "keyword"},
    "direction": {"type": "keyword"}, "detected_at": {"type": "date"},
    "resolution": {"type": "keyword"}, "method": {"type": "keyword"}, "baseline_source": {"type": "keyword"},
    "confidence": {"type": "keyword"}, "p": {"type": "double"},
    "status": {"type": "keyword"}, "streak": {"type": "integer"},
    "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "alert_id": {"type": "keyword"},   # 併進哪一件（investigator alert 寫回；儀表板依它撈這件的異常）
    # 哪一版 engine 產生的（aiops.ENGINE_VERSION；AnomalyRepository.write 補）。放在這裡而不是 RULE_FIELDS：index 是 dynamic false，
    # 但 ensure_index 對既有 index 也會 PUT _mapping 把這份 properties 補上，detect、rules、peer、quality、trace 每輪都呼叫，不用另外補
    "engine_version": {"type": "keyword"},
}}}
# 規則、同儕、品質、trace 的偵測多寫的三個欄位（既有索引由 AnomalyRepository.ensure 補上）
RULE_FIELDS = {"severity": {"type": "keyword"}, "message": {"type": "text"}, "rule": {"type": "keyword"}}


@dataclass
class Anomaly:
    """寫進 aiops-anomalies 的一筆異常。**這是異常文件唯一的定義處**：同一檔的 ANOMALY_MAPPING、detector/common.py 的
    RULE_FIELDS 要跟它一致（tests/test_detector_base.py 會對）。to_doc() 產生寫入的 dict。

    欄位            誰寫                      意思
    ts→@timestamp   全部                      異常落在哪個時間格：rules／quality 是 now 取整到 interval、peer 到分、baseline／trace 是資料點的時間
    series          全部                      哪條序列或規則（rules 是 rule.<名>、trace 的規則是 trace.segment.<名>）
    keys            全部                      對象（服務、Pod…）；rules 多帶 context 欄位、quality 缺欄位時多一個 field
    value           全部                      這次的值
    level           全部                      rule／peer／quality；baseline 是 dow_hour／hour／all／group
    median          全部（沒有就 None）       平常的值；quality 放昨天的值
    expected_low/   全部（沒有就 None）       正常範圍；rules 用 expected_high 放門檻
      expected_high
    z               全部（沒有就 None）       偏離幾倍
    direction       全部                      up／down／both；rules 放 op（>=、<=）
    detected_at     全部                      寫入的時間（沒給就在 to_doc 時補）
    rule            rules、peer、quality、trace 規則   規則名（RULE_FIELDS 的欄位；peer、quality 放檢查名）
    severity        同上                      warning／critical
    message         同上                      給人看的一句話（Grafana 直接顯示）
    since           rules                     從什麼時候開始超過門檻（只在 _source，沒進 mapping）
    resolution      baseline                  粒度（30s…1h；粗粒度沒有時是 None，但鍵一定在）
    method          baseline                  mad／poisson
    baseline_source baseline                  self 或 group:<組名>
    confidence      baseline                  high／medium／low
    status          baseline                  anomaly／warning（連續幾點才算；cmd_baseline.with_status）
    streak、p       baseline（有才寫）        連續第幾個、卜瓦松的機率
    不在這裡：dept、group、engine_version 由 AnomalyRepository.write 補（detect/store/owner_repo.py 查歸屬）；alert_id 由 investigator 併件時寫回。
    """
    ts: pd.Timestamp
    series: str
    keys: dict
    value: float
    level: str
    median: float | None = None
    expected_low: float | None = None
    expected_high: float | None = None
    z: float | None = None
    direction: str | None = None
    detected_at: str | None = None
    rule: str | None = None
    severity: str | None = None
    message: str | None = None
    since: pd.Timestamp | None = None
    resolution: str | None = None
    method: str | None = None
    baseline_source: str | None = None
    confidence: str | None = None
    status: str | None = None
    streak: int | None = None
    p: float | None = None

    # 一定寫（沒有就 None）；其餘有值才寫。resolution 跟著 method 一起寫：baseline 的異常粗粒度時 resolution 是 None、鍵仍要在
    ALWAYS = ("median", "expected_low", "expected_high", "z", "level", "direction")

    def id(self) -> str:
        """預設的 _id：同一條序列、同一個對象、同一個時間格只有一筆（重跑會覆寫）。"""
        return doc_id(self.series, self.keys, self.ts)

    def to_doc(self, detected_at: str | None = None) -> dict:
        d = {"@timestamp": self.ts.isoformat(), "series": self.series}
        if self.rule is not None:
            d["rule"] = self.rule
        d.update({"keys": self.keys, "value": self.value})
        d.update({f: getattr(self, f) for f in self.ALWAYS})
        for f in ("severity", "message"):
            if getattr(self, f) is not None:
                d[f] = getattr(self, f)
        if self.since is not None:
            d["since"] = self.since.isoformat()
        d["detected_at"] = self.detected_at or detected_at or pd.Timestamp.now(tz="UTC").isoformat()
        if self.method is not None:
            d["resolution"] = self.resolution
        for f in ("method", "baseline_source", "confidence", "status", "streak", "p"):
            if getattr(self, f) is not None:
                d[f] = getattr(self, f)
        return d


# ── 一筆異常歸到哪一件：keys 裡哪個欄位是「對象」由場景給（Scenario.entity_keys）；namespace、節點是共通的 fallback ──
NS_KEYS = ("resource.attributes.k8s.namespace.name", "k8s.namespace.name", "namespace", "kubernetes.namespace")
NODE_KEYS = ("resource.attributes.k8s.node.name", "k8s.node.name", "node")


def entity_of(a: dict, entity_keys: tuple[str, ...]) -> tuple[str, str]:
    """(類別, 名稱)：這筆異常歸到哪一件。對象（服務／機台）優先；沒有的用 namespace、再用節點；資料品質另外一類（送平台）。"""
    keys = a.get("keys") or {}
    cat = "data_quality" if a.get("level") == "quality" else "service"
    for kind, fields in (("service", entity_keys), ("namespace", NS_KEYS), ("node", NODE_KEYS)):
        for f in fields:
            if keys.get(f):
                return (cat if kind == "service" or cat == "data_quality" else kind), str(keys[f])
    if cat == "data_quality" and keys:
        return cat, "、".join(str(v) for v in keys.values())
    return cat if cat == "data_quality" else "other", a.get("series", "?")


def entity_value(keys: dict | None, entity_keys: tuple[str, ...]) -> str | None:
    """keys 裡的對象名稱（服務／機台）；沒有回 None。"""
    for k in entity_keys:
        if (keys or {}).get(k):
            return str(keys[k])
    return None


# ── baseline ──
BASELINE_INDEX = "aiops-baseline"
# baseline 讀回來的欄位。method 存成 baseline_method：序列的 keys 也可能有 method（HTTP method），會撞名
BASELINE_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution", "baseline_method", "baseline_source", "trained_at")
GROUP_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution")
RENAMED = {"baseline_method": "method"}
BASELINE_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "series": {"type": "keyword"}, "keys": KEYS_MAPPING, "dow": {"type": "byte"}, "hour": {"type": "byte"},
    "level": {"type": "keyword"}, "median": {"type": "double"}, "mad": {"type": "double"},
    "p05": {"type": "double"}, "p95": {"type": "double"}, "n": {"type": "integer"},
    "trained_at": {"type": "date"}, "history_from": {"type": "date"}, "history_to": {"type": "date"},
    "resolution": {"type": "keyword"}, "method": {"type": "keyword"}, "baseline_source": {"type": "keyword"},
    "events": {"type": "double"},
}}}


# ── trace ──
_SPAN_FIELDS = ["@timestamp", "trace_id", "span_id", "parent_span_id", "name", "kind", "duration", "status.code", "links",
                "resource.attributes", "attributes"]
# APM 的文件：transaction、span 都是物件（span.stacktrace 很大、不要）；labels 是 agent 設的標籤（dept、group、業務 ID）
_APM_FIELDS = {"includes": ["@timestamp", "timestamp.us", "processor.event", "trace.id", "parent.id", "transaction", "span",
                            "event.outcome", "service", "kubernetes", "host.name", "labels", "data_stream.namespace",
                            "http.request.method", "http.response.status_code", "url.path"],
               "excludes": ["span.stacktrace", "transaction.custom", "transaction.marks"]}


MERGED_LIKE = ("traces-merged", "lot-paths")   # engine 合成、一個請求（一個 lot）一筆的索引；lot-paths 是工廠的履歷（plugins/factory/paths.py）


def schema_of(index: str, source: str = "otel") -> str:
    """merged（engine 合成的：traces-merged-*、lot-paths-*）、apm（Elastic APM 的 traces-apm-*；原始資料格式 source 是 apm 時其他 raw 索引也當 APM）、
    otel（Collector 寫的 raw）。source 是場景的原始資料格式（Scenario.source）。"""
    if any(m in index for m in MERGED_LIKE):
        return "merged"
    if "apm" in index or source == "apm":
        return "apm"
    return "otel"


def is_merged(index: str) -> bool:
    return schema_of(index) == "merged"


def trace_id_field(index: str, source: str = "otel") -> str:
    return "trace.id" if schema_of(index, source) == "apm" else "trace_id"


def _duration_ms(v) -> float:
    """奈秒 → 毫秒。壞 SDK 送來的格式錯誤值（字串、負數）當 0，不能讓一筆壞 span 擋住整批合併或判斷。"""
    try:
        d = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return d / 1e6 if d > 0 else 0.0


def _start_ms(ts) -> float:
    # ES 回的 @timestamp 可能是數字、數字的字串（奈秒精度時，例如 "1791687912264.916000"），或 ISO 格式
    if isinstance(ts, str) and ts.replace(".", "", 1).isdigit():
        ts = float(ts)
    return float(ts) if isinstance(ts, (int, float)) else datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000


def is_apm_doc(src: dict) -> bool:
    return get_field(src, "processor.event") in ("transaction", "span") or get_field(src, "trace.id") is not None


def _apm_kind(event: str, obj: dict) -> str:
    """APM 沒有 OTel 的 kind，從文件種類推：transaction 是入口（HTTP 請求 Server、訊息處理 Consumer）；
    span 有往外的對象（DB、下游、外部系統）是 Client，送訊息是 Producer，其餘是程式內部的段（Internal）。"""
    typ = (get_field(obj, "type") or "").split(".")[0]
    if event == "transaction":
        return "Consumer" if typ == "messaging" else "Server"
    if typ == "messaging" and (get_field(obj, "action") or "") in ("send", "publish", "produce"):
        return "Producer"
    if typ in ("db", "external", "storage", "messaging") or get_field(obj, "destination.service.resource"):
        return "Client"
    return "Internal"


def _apm_span(src: dict) -> dict:
    """Elastic APM 的 transaction／span 文件 → 跟 to_span 同一種格式。
    耗時是微秒（*.duration.us）；錯誤看 event.outcome；對象是 span.destination.service.resource（"oracle"、"mes-api:8080"）；
    span 本身沒有例外（例外是 logs-apm.error 的獨立文件，先不接）。attrs、resource 攤平成 OTel 的名字，根因分析的
    commonality（k8s.pod.name、db.statement）兩種來源同一套設定。"""
    event = get_field(src, "processor.event") or ("transaction" if get_field(src, "transaction.duration.us") is not None else "span")
    obj = (src.get(event) if isinstance(src.get(event), dict) else {k[len(event) + 1:]: v for k, v in src.items() if k.startswith(event + ".")}) or {}
    labels = src.get("labels") or {}
    us = get_field(src, "timestamp.us")
    attrs = {k: v for k, v in {
        "db.statement": get_field(obj, "db.statement"), "db.system": get_field(obj, "subtype") if (get_field(obj, "type") or "").startswith("db") else None,
        "http.request.method": get_field(src, "http.request.method"), "http.response.status_code": get_field(src, "http.response.status_code"),
        "url.path": get_field(src, "url.path"), f"{event}.type": get_field(obj, "type"), "span.subtype": get_field(obj, "subtype"),
        "transaction.result": get_field(obj, "result"),
        **{f"labels.{k}": v for k, v in labels.items()}}.items() if v is not None}
    resource = {k: v for k, v in {
        "service.name": get_field(src, "service.name"), "service.version": get_field(src, "service.version"),
        "service.environment": get_field(src, "service.environment"),
        "k8s.pod.name": get_field(src, "kubernetes.pod.name"), "k8s.namespace.name": get_field(src, "kubernetes.namespace"),
        "k8s.node.name": get_field(src, "kubernetes.node.name") or get_field(src, "host.name"), "host.name": get_field(src, "host.name"),
        "dept": labels.get("dept"), "group": labels.get("group"), "data_stream.namespace": get_field(src, "data_stream.namespace")}.items() if v is not None}
    return {"trace_id": get_field(src, "trace.id"), "span_id": get_field(obj, "id"), "parent": get_field(src, "parent.id") or None,
            "service": get_field(src, "service.name") or "?", "name": get_field(obj, "name") or "?",
            "kind": _apm_kind(event, obj), "target": get_field(obj, "destination.service.resource") or get_field(src, "service.target.name"),
            "start_ms": us / 1000.0 if isinstance(us, (int, float)) else _start_ms(src["@timestamp"]),
            "dur_ms": _duration_ms(get_field(obj, "duration.us")) * 1000.0,     # 微秒 → 毫秒（_duration_ms 是奈秒 → 毫秒）
            "error": get_field(src, "event.outcome") == "failure", "exception": None,
            "links": [sid for l in (get_field(obj, "links") or []) if (sid := get_field(l, "span.id"))],
            "attrs": attrs, "resource": resource}


def to_span(src: dict) -> dict:
    if is_apm_doc(src):
        return _apm_span(src)
    attrs = src.get("attributes") or {}
    # db.system.name：新版 DB 語意（舊的 db.system 改名）；沒有對象的 Client span 段名沒有「→」，N+1 規則看不到
    target = (get_field(attrs, "peer.service") or get_field(attrs, "db.system") or get_field(attrs, "db.system.name")
              or get_field(attrs, "server.address"))
    return {"trace_id": src["trace_id"], "span_id": src["span_id"], "parent": src.get("parent_span_id") or None,
            "service": get_field(src, "resource.attributes.service.name") or "?", "name": src.get("name", "?"),
            "kind": src.get("kind", ""), "target": target, "start_ms": _start_ms(src["@timestamp"]),
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

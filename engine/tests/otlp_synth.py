"""合成資料用。兩條路：

1. metric_doc＋bulk：直接用 _bulk 寫出跟 Collector 的 elasticsearch 寫入元件在 ES 8.19 寫出來一樣的 metrics 文件
   （k3d 實測的 _source：直方圖是 histogram {"values": […], "counts": […]}、沒有 temporality、sum 另存成 <名稱>_sum、
   _metric_names_hash、start_timestamp；docs/pdca/meta/2026-10-metrics-es8-shape.md）。ES 8、9 都能寫。
2. metrics_request、logs_request＋post：編成 OTLP protobuf 送 ES 9 自己的 OTLP 入口（/_otlp/v1/metrics、/_otlp/v1/logs）。
   **ES 8.19 沒有這個入口**，只有 ES 9 能用；log 的合成資料目前只有這條路。
   ES 只收 protobuf（JSON 回 406），venv 沒有 protobuf 套件，這裡手寫最小的編碼器。

只編得出這幾種訊息（欄位編號照 opentelemetry-proto v1）：
  metrics：ResourceMetrics → ScopeMetrics → Metric(histogram, delta) → HistogramDataPoint
  logs   ：ResourceLogs → ScopeLogs → LogRecord（severity_text、body 字串）
"""
from __future__ import annotations

import json
import struct
import time

import numpy as np

# Java agent、.NET 自動埋點 http.server.request.duration 的預設邊界（秒）：docs/pdca/phase1-collect/2026-10-field-unify.md
HTTP_BOUNDS = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0]
DELTA = 1


def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _len(field: int, payload: bytes) -> bytes:
    return _key(field, 2) + _varint(len(payload)) + payload


def _str(field: int, s: str) -> bytes:
    return _len(field, s.encode("utf-8"))


def _fixed64(field: int, n: int) -> bytes:
    return _key(field, 1) + struct.pack("<Q", n)


def _double(field: int, x: float) -> bytes:
    return _key(field, 1) + struct.pack("<d", x)


def _uint(field: int, n: int) -> bytes:
    return _key(field, 0) + _varint(n)


def _kv(k: str, v) -> bytes:
    if isinstance(v, bool):
        any_ = _uint(2, int(v))
    elif isinstance(v, int):
        any_ = _uint(3, v)
    elif isinstance(v, float):
        any_ = _double(4, v)
    else:
        any_ = _str(1, str(v))
    return _str(1, k) + _len(2, any_)


def _attrs(field: int, attrs: dict) -> bytes:
    return b"".join(_len(field, _kv(k, v)) for k, v in attrs.items())


def histogram_point(attrs: dict, start_ns: int, t_ns: int, counts: list[int], total: float,
                    bounds=HTTP_BOUNDS, vmin: float | None = None, vmax: float | None = None) -> bytes:
    """HistogramDataPoint：attributes=9、start=2、time=3、count=4、sum=5、bucket_counts=6（packed fixed64）、
    explicit_bounds=7（packed double）、min=11、max=12。"""
    n = int(sum(counts))
    out = _attrs(9, attrs) + _fixed64(2, start_ns) + _fixed64(3, t_ns) + _fixed64(4, n) + _double(5, float(total))
    out += _len(6, b"".join(struct.pack("<Q", int(c)) for c in counts))
    out += _len(7, b"".join(struct.pack("<d", b) for b in bounds))
    if n and vmin is not None:
        out += _double(11, float(vmin)) + _double(12, float(vmax))
    return out


def metrics_request(groups: list[tuple[dict, list[bytes]]], name: str = "http.server.request.duration",
                    unit: str = "s") -> bytes:
    """groups：[(resource attributes, [HistogramDataPoint, …])]。每個 resource 一個 ResourceMetrics。"""
    rms = b""
    for res, points in groups:
        hist = b"".join(_len(1, p) for p in points) + _uint(2, DELTA)
        metric = _str(1, name) + _str(3, unit) + _len(9, hist)
        scope = _len(1, _str(1, "bedge-synth")) + _len(2, metric)
        rms += _len(1, _len(1, _attrs(1, res)) + _len(2, scope))
    return rms


def log_record(t_ns: int, severity: str, body: str, attrs: dict | None = None) -> bytes:
    """LogRecord：time=1、severity_text=3、body=5（AnyValue string）、attributes=6、observed_time=11。"""
    sev_num = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}.get(severity.upper(), 9)
    out = _fixed64(1, t_ns) + _uint(2, sev_num) + _str(3, severity) + _len(5, _str(1, body)) + _fixed64(11, t_ns)
    return out + _attrs(6, attrs or {})


def logs_request(groups: list[tuple[dict, list[bytes]]]) -> bytes:
    out = b""
    for res, records in groups:
        scope = _len(1, _str(1, "bedge-synth")) + b"".join(_len(2, r) for r in records)
        out += _len(1, _len(1, _attrs(1, res)) + _len(2, scope))
    return out


def es_histogram(counts: list[int], bounds) -> dict:
    """Collector 的 elasticsearch 寫入元件把 explicit bucket 直方圖寫成 ES histogram 的樣子（k3d 上 ES 8.19 實際寫出來的文件：
    邊界 0.025／0.05／0.075 之間的格變成 values 0.0375、0.0625）：每個有值的 bucket 一個點，位置是 bucket 中點
    （第一格的下界是 0；超過最大邊界那格放在最大邊界），0 的格不寫。min、max、sum 都不在直方圖裡。"""
    vals, cnts = [], []
    for i, c in enumerate(counts):
        if not c:
            continue
        lo = 0.0 if i == 0 else bounds[i - 1]
        vals.append(bounds[-1] if i == len(bounds) else (lo + bounds[i]) / 2)
        cnts.append(int(c))
    return {"values": vals, "counts": cnts}


def bucketize(latencies: np.ndarray, bounds=HTTP_BOUNDS) -> list[int]:
    """每個請求的延遲（秒）→ explicit bucket 的次數（len(bounds)+1 格，最後一格是 > 最大邊界）。"""
    idx = np.searchsorted(np.asarray(bounds), latencies, side="left")
    return np.bincount(idx, minlength=len(bounds) + 1).tolist()


def post(es, path: str, payload: bytes) -> None:
    r = es.s.post(f"{es.url}{path}", data=payload, headers={"Content-Type": "application/x-protobuf"}, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"{path} → {r.status_code}: {r.text[:300]}")
    # 部分拒收時 ES 回 200 但帶 partial_success（protobuf）；有內容就當失敗，免得默默少資料
    if r.content and len(r.content) > 2:
        raise RuntimeError(f"{path} 部分拒收：{r.content[:300]!r}")


# ── 同樣的文件改用 _bulk 寫（可以帶 _id，逾時重送不會重複）──

METRIC = "metrics.http.server.request.duration"
NAMES_HASH = "30292700b84da900"   # _metric_names_hash：Collector 依文件裡的指標名稱算的；合成資料全部一樣，照 k3d 上實際那筆


def metric_doc(res: dict, attrs: dict, start_ns: int, t_ns: int, counts: list[int], total: float, vmin: float, vmax: float,
               names_hash: str = NAMES_HASH) -> dict:
    """跟 Collector 寫進 ES 8.19 的 _source 一樣的文件（data_stream.* 從 resource 拿出來放上層）。
    直方圖不帶 min、max、sum；sum 另放 http.server.request.duration_sum（node.yaml transform/hist-sum）。vmin、vmax 留著給舊的呼叫端，不用。"""
    r = {k: v for k, v in res.items() if not k.startswith("data_stream.")}
    return {"@timestamp": t_ns // 1_000_000, "start_timestamp": start_ns // 1_000_000,
            "resource": {"attributes": r},
            "data_stream": {"type": "metrics", "dataset": res["data_stream.dataset"] + ".otel", "namespace": res["data_stream.namespace"]},
            "scope": {"name": "bedge-synth"}, "_metric_names_hash": names_hash, "attributes": attrs,
            "metrics": {"http.server.request.duration": es_histogram(counts, HTTP_BOUNDS), "http.server.request.duration_sum": float(total)}}


def _retry(fn, what: str, tries: int = 8):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001  逾時、連線被重設：等一下再送（帶 _id，重送不會重複）
            if i == tries - 1:
                raise
            print(f"    {what} 失敗（{type(e).__name__}），{30 * (i + 1)} 秒後重送", flush=True)
            time.sleep(30 * (i + 1))


def bulk(es, index: str, docs: list[tuple[str, dict]], chunk: int = 2000) -> None:
    """op_type create＋固定 _id：已經有的（409）算成功，所以逾時重送是安全的。"""
    for i in range(0, len(docs), chunk):
        lines = []
        for _id, d in docs[i:i + chunk]:
            lines.append(json.dumps({"create": {"_index": index, "_id": _id,
                                                "dynamic_templates": {METRIC: "histogram", METRIC + "_sum": "gauge_double"}}}))
            lines.append(json.dumps(d))
        payload = "\n".join(lines) + "\n"

        def send():
            r = es.s.post(f"{es.url}/_bulk", data=payload, headers={"Content-Type": "application/x-ndjson"}, timeout=900)
            body = r.json()
            bad = [it["create"] for it in body.get("items", []) if it["create"].get("status") not in (201, 409)]
            if r.status_code != 200 or bad:
                raise RuntimeError(f"bulk {r.status_code}：{bad[:2] or r.text[:300]}")
        _retry(send, f"bulk {index} 第 {i} 筆起")


def post_retry(es, path: str, payload: bytes) -> None:
    """OTLP 重送：log 的 _id 是內容指紋（k8s-logs-otel），重送的那幾筆 ES 回 409、不會重複，所以部分拒收不算錯。"""
    def send():
        r = es.s.post(f"{es.url}{path}", data=payload, headers={"Content-Type": "application/x-protobuf"}, timeout=900)
        if r.status_code != 200:
            raise RuntimeError(f"{path} → {r.status_code}: {r.text[:300]}")
    _retry(send, path)

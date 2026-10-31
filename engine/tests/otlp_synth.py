"""合成資料用：直接用 _bulk 寫出跟 Collector 的 elasticsearch 寫入元件（otel mode）寫進 ES 8.19 一樣的文件。

  metrics：metric_doc → metrics-<dataset>.otel-<namespace>（k3d 實測的 _source：直方圖是 histogram
           {"values": […], "counts": […]}、沒有 temporality、sum 另存成 <名稱>_sum、_metric_names_hash、start_timestamp；
           docs/pdca/meta/2026-10-metrics-es8-shape.md）
  log    ：log_doc → logs-generic.otel-<namespace>，由 default pipeline k8s-logs-otel 轉到 k8s-<group>-<cluster>，
           跟 Collector 送來的走同一條（_id 是 pipeline 算的內容指紋，重送不重複）

ES 8.19 沒有 ES 9 的 OTLP 入口（/_otlp/v1/*），所以不編 OTLP protobuf。
"""
from __future__ import annotations

import json
import time

import numpy as np

# Java agent、.NET 自動埋點 http.server.request.duration 的預設邊界（秒）：docs/pdca/phase1-collect/2026-10-field-unify.md
HTTP_BOUNDS = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0]


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


METRIC = "metrics.http.server.request.duration"
METRIC_TEMPLATES = {METRIC: "histogram", METRIC + "_sum": "gauge_double"}
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


def log_doc(res: dict, t_ns: int, severity: str, body: str, attrs: dict | None = None) -> dict:
    """應用用 OTLP 直送的一筆 log（沒有 log.iostream，pipeline 記成 log.source: otlp），寫進 logs-generic.otel-<namespace>。
    形狀照 elasticsearch 寫入元件 otel mode：@timestamp 毫秒、body.text、severity_text、resource.attributes、attributes。"""
    r = {k: v for k, v in res.items() if not k.startswith("data_stream.")}
    return {"@timestamp": t_ns // 1_000_000, "observed_timestamp": t_ns // 1_000_000,
            "data_stream": {"type": "logs", "dataset": "generic.otel", "namespace": res["data_stream.namespace"]},
            "resource": {"attributes": r}, "scope": {"name": "bedge-synth"}, "attributes": attrs or {},
            "severity_text": severity, "body": {"text": body}}


def _retry(fn, what: str, tries: int = 8):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001  逾時、連線被重設：等一下再送（帶 _id，重送不會重複）
            if i == tries - 1:
                raise
            print(f"    {what} 失敗（{type(e).__name__}），{30 * (i + 1)} 秒後重送", flush=True)
            time.sleep(30 * (i + 1))


def bulk(es, index: str, docs: list[tuple[str | None, dict]], chunk: int = 2000, templates: dict | None = METRIC_TEMPLATES) -> None:
    """op_type create＋固定 _id（_id 是 None 時由 pipeline 算）：已經有的（409）算成功，所以逾時重送是安全的。"""
    for i in range(0, len(docs), chunk):
        lines = []
        for _id, d in docs[i:i + chunk]:
            meta = {"_index": index, **({"_id": _id} if _id is not None else {}), **({"dynamic_templates": templates} if templates else {})}
            lines.append(json.dumps({"create": meta}))
            lines.append(json.dumps(d))
        payload = "\n".join(lines) + "\n"

        def send():
            r = es.s.post(f"{es.url}/_bulk", data=payload, headers={"Content-Type": "application/x-ndjson"}, timeout=900)
            body = r.json()
            bad = [it["create"] for it in body.get("items", []) if it["create"].get("status") not in (201, 409)]
            if r.status_code != 200 or bad:
                raise RuntimeError(f"bulk {r.status_code}：{bad[:2] or r.text[:300]}")
        _retry(send, f"bulk {index} 第 {i} 筆起")

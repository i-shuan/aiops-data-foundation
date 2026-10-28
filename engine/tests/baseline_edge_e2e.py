"""動態基準線學習的邊緣情境、誤報率、learning loop 調整介面：端到端測試（docs/pdca/phase2-anomaly/2026-10-baseline-verify.md）。

資料的形狀跟 Collector 寫出來的一樣（tests/otlp_synth.py）：
  metrics → metrics-bedge.otel-it、metrics-btune.otel-it：http.server.request.duration 直方圖（ES 的 OTLP 入口轉成
            exponential_histogram）、resource.attributes.service.name 是 Pod label 的值、30 秒一筆、delta。
            用 _bulk 寫 OTLP 入口寫出來的同一份 _source（帶 _id，ES 慢、逾時重送不會重複）；開頭先各寫一份比對，
            _source 逐欄一樣、p50/p95/p99 一樣才繼續（check_bulk_matches_otlp）。
  log     → 走 /_otlp/v1/logs → logs-generic.otel-it → k8s-logs-otel pipeline → k8s-it-bedge（_id 是內容指紋，重送不重複）
metrics 的 data stream 用 standard mode（同一組 component template）：TSDS 的 look_back_time 上限 7 天，寫不進 4 週前的資料；
欄位型別、聚合結果一樣（baseline 只用聚合）。

序列定義從 configs/baseline/series.yaml 複製（http.*、log.*，名稱加 edge./tune. 前綴、index 改成上面的），全域設定照抄。

時間（台北）：
  09-28（週一）起  svc-rare（卜瓦松）、svc-logs（log）
  10-05 起         其他 30 秒的服務；新服務各自在 10-25、10-23、10-19、10-12 上線
  10-12 10:00～16:00、10-26 15:00～16:00  收集中斷（所有資料都沒有）
  10-19～10-26     誤報率：每天 train 一次、detect 隔天（沒有注入異常的服務）
  10-26（週一）    邊緣情境：train 到 10-26 00:00，detect 10-26 一整天（注入的異常都在這天）

執行（在 engine 底下）：ES_URL=http://127.0.0.1:31502 python -m tests.baseline_edge_e2e [--skip-write] [--only fp,edge,reset,tune]
寫入可以中斷後重跑（已經寫完的服務跳過；寫到一半的用同一個 _id 補）。
會建立／覆蓋上面三個 data stream，以及 aiops-baseline、aiops-anomalies 裡 series 以 edge.、tune. 開頭的資料、aiops-tuning 裡 by=bedge 的。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile

import numpy as np
import pandas as pd
import yaml

from detector.__main__ import main as cli
from detector.cmd_baseline import detect_series
from detector.common import keys_of
from detector.es import ANOMALY_INDEX, BASELINE_INDEX, ES, load_baseline
from detector.model import DetectOptions
from detector.tuning import TUNING_INDEX, data_adjustments, load_overrides
from tests import otlp_synth as O


def _patient_es():
    """測試機很慢時（多個 ES 共用一台 VM，單一查詢可以超過 2 分鐘），engine 的 ES 用戶端逾時（120 秒）就整輪失敗。
    這裡只在測試裡把逾時拉長、逾時或連線被重設時重送（查詢、帶 _id 的寫入、delete_by_query 重送都不會多出資料）。"""
    import requests
    from core.es import ES as _ES

    def req(self, method, path, body=None, ok=(200, 201)):
        for i in range(6):
            try:
                r = self.s.request(method, self.url + path, json=body, timeout=900)
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                if i == 5:
                    raise
                print(f"    （ES 沒回應：{type(e).__name__}，{30 * (i + 1)} 秒後重送 {method} {path[:60]}）", flush=True)
                import time
                time.sleep(30 * (i + 1))
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    orig_bulk = _ES.bulk_index

    def bulk_index(self, index, docs, chunk=2000):
        return O._retry(lambda: orig_bulk(self, index, docs, chunk), f"bulk_index {index}")
    orig_ensure = _ES.ensure_index

    def ensure_index(self, name, body):
        return O._retry(lambda: orig_ensure(self, name, body), f"ensure_index {name}")
    _ES.req, _ES.bulk_index, _ES.ensure_index = req, bulk_index, ensure_index


_patient_es()

TZ = "Asia/Taipei"
D0 = pd.Timestamp("2026-09-28", tz=TZ)          # 週一：卜瓦松、log 的歷史起點（4 週）
F0 = pd.Timestamp("2026-10-05", tz=TZ)          # 30 秒服務的起點（細粒度只學 14 天）
FP_START = pd.Timestamp("2026-10-19", tz=TZ)    # 誤報率那一週
T_END = pd.Timestamp("2026-10-26", tz=TZ)       # 週一：邊緣情境 train 的終點、detect 那天
DATA_END = T_END + pd.Timedelta(days=1)
OUTAGES = [(pd.Timestamp("2026-10-12 10:00", tz=TZ), pd.Timestamp("2026-10-12 16:00", tz=TZ)),
           (pd.Timestamp("2026-10-26 15:00", tz=TZ), pd.Timestamp("2026-10-26 16:00", tz=TZ))]
STEP_AT = pd.Timestamp("2026-10-22 10:00", tz=TZ)
BY = "bedge"
EDGE_DS, TUNE_DS = "bedge", "btune"
LOG_INDEX = "k8s-it-bedge"
STEP = pd.Timedelta("30s")


def at(hhmm: str, day=T_END) -> pd.Timestamp:
    return day + pd.Timedelta(hhmm + (":00" if hhmm.count(":") == 1 else ""))


# ── 流量形狀（每 30 秒的請求數 λ，台北時間） ──

def seasonal(local: pd.DatetimeIndex, peak: float, ramp_min: int = 8 * 60) -> np.ndarray:
    """平日 ramp～20:00 白天（peak）、其他時間 0.2 倍；9 點那一小時 1.5 倍（上班尖峰）；23:00～01:00 批次 +1 倍（跨午夜）；週末全部 0.5 倍。"""
    m = local.hour * 60 + local.minute
    lam = np.where((m >= ramp_min) & (local.hour < 20), peak, 0.2 * peak)
    lam = np.where(local.hour == 9, 1.5 * peak, lam)
    lam = lam + np.where((local.hour == 23) | (local.hour == 0), peak, 0.0)
    return lam * np.where(local.day_of_week >= 5, 0.5, 1.0)


# 服務：名稱 → 設定。lat 是每個請求的平均延遲（秒，gamma 分布，p95 約 2.4 倍）
SERVICES = {
    "svc-season": dict(route="/api/orders", start=F0, shape="season", peak=40, err=0.005),
    "svc-peak":   dict(route="/api/shift", start=F0, shape="season", peak=40, err=0.005, ramp=8 * 60 + 45),
    "svc-flat":   dict(route="/api/status", start=F0, shape="flat", n=30, lat_fixed=0.03),
    "svc-step":   dict(route="/api/lots", start=pd.Timestamp("2026-10-12", tz=TZ), shape="const", peak=40),
    "svc-spike":  dict(route="/api/eqp", start=F0, shape="const", peak=40, drift=0.01),   # 延遲每天 +1%（緩慢漂移）
    "svc-rare":   dict(route="/api/report", start=D0, shape="const", peak=0.05),
    "svc-new1d":  dict(route="/api/new", start=pd.Timestamp("2026-10-25", tz=TZ), shape="season", peak=40),
    "svc-new3d":  dict(route="/api/new", start=pd.Timestamp("2026-10-23", tz=TZ), shape="season", peak=40),
    "svc-new1w":  dict(route="/api/new", start=pd.Timestamp("2026-10-19", tz=TZ), shape="season", peak=40),
    "svc-new2w":  dict(route="/api/new", start=pd.Timestamp("2026-10-12", tz=TZ), shape="season", peak=40),
}
TUNE_SERVICES = {
    "svc-ta": dict(route="/api/t", start=pd.Timestamp("2026-10-12", tz=TZ), shape="const", peak=40),
    "svc-tb": dict(route="/api/t", start=pd.Timestamp("2026-10-12", tz=TZ), shape="const", peak=40),
}
FP_SERVICES = ["svc-season", "svc-peak", "svc-flat", "svc-spike", "svc-rare", "svc-logs"]
NEW = {"svc-new1d": "all", "svc-new3d": "hour", "svc-new1w": "hour", "svc-new2w": "dow_hour"}

# 10-26 注入的異常：(服務, 開始, 結束, 欄位, 怎麼改)。欄位 n＝請求數（加多少或乘多少）、lat＝延遲倍數、err＝錯誤數、lat_fixed＝固定延遲
INJECT = [
    ("svc-season", "11:00", "11:10", "n_mul", 0.1),         # 白天流量掉到一成
    ("svc-flat", "10:00", "10:05", "n_mul", 1.1),           # MAD=0：+10%，不該報
    ("svc-flat", "11:00", "11:05", "n_mul", 1.4),           # MAD=0：+40%，要報
    ("svc-flat", "14:00", "14:05", "lat_fixed", 0.3),       # MAD=0：延遲換一格
    ("svc-flat", "13:00", "13:00:30", "err_n", 1),          # 錯誤率 0 → 1/30（3%，不到 min_abs 5%）
    ("svc-flat", "13:30", "13:30:30", "err_n", 10),         # 錯誤率 0 → 33%
    ("svc-spike", "10:00", "10:00:30", "n_add", 70),        # 單點、z 約 8 → warning
    ("svc-spike", "10:30", "10:30:30", "n_add", 300),       # 單點、z 約 33 ≥ instant_z → anomaly
    ("svc-spike", "11:00", "11:00:30", "n_add", 70),        # 連續兩點
    ("svc-spike", "11:01", "11:01:30", "n_add", 70),
    ("svc-spike", "11:30", "11:30:30", "n_add", 70),        # 隔一點的兩點
    ("svc-spike", "11:32", "11:32:30", "n_add", 70),
    ("svc-spike", "12:30", "12:30:30", "err_frac", 0.5),    # 錯誤率單點 50%（confirm_points 1）
    ("svc-rare", "14:00", "15:00", "n_extra", 24),          # 稀疏：平常每小時 6 次，這小時多 24 次
    ("svc-rare", "10:00", "11:00", "n_extra", 6),           # 稀疏：多 6 次（P≈0.02，不該報）
    ("svc-step", "16:30", "16:35", "lat_mul", 3.0),         # 換版後的新水準上再變慢（reset_point 後要抓到）
] + [(s, "14:00", "14:10", "lat_mul", 5.0) for s in NEW]
TUNE_INJECT = [(s, "10:00", "10:05", "lat_mul", 3.0) for s in TUNE_SERVICES] + \
              [(s, "11:00", "11:00:30", "n_add", 70) for s in TUNE_SERVICES]
LOG_BURST = ("20:00", 15)   # svc-logs 10-26 20:00 那一小時多 15 筆 error（平常每小時 1.2 筆）


def in_outage(ts: pd.DatetimeIndex) -> np.ndarray:
    out = np.zeros(len(ts), dtype=bool)
    for a, b in OUTAGES:
        out |= (ts >= a) & (ts < b)
    return out


def make_service(name: str, spec: dict, inject: list, rng) -> dict:
    """每 30 秒一個點：請求數、錯誤數、延遲倍數（或固定延遲）。回傳 numpy 陣列。"""
    ts = pd.date_range(spec["start"], DATA_END, freq=STEP, inclusive="left")
    if spec["shape"] == "flat":
        n = np.full(len(ts), float(spec["n"]))
    else:
        lam = seasonal(ts, spec["peak"], spec.get("ramp", 8 * 60)) if spec["shape"] == "season" else np.full(len(ts), spec["peak"])
        n = None
    lat_mul = np.ones(len(ts))
    if spec.get("drift"):
        lat_mul *= 1 + spec["drift"] * ((ts - F0) / pd.Timedelta(days=1)).to_numpy(dtype=float)
    if name == "svc-step":
        lat_mul *= np.where(ts >= STEP_AT, 2.0, 1.0)
    lat_fixed = np.full(len(ts), spec.get("lat_fixed", np.nan))
    err_n = np.full(len(ts), -1.0)        # -1：照錯誤率抽
    err_frac = np.full(len(ts), np.nan)
    extra = np.zeros(len(ts))
    n_mul, n_add = np.ones(len(ts)), np.zeros(len(ts))
    for svc, a, b, field, v in inject:
        if svc != name:
            continue
        w = (ts >= at(a)) & (ts < at(b))
        if field == "n_mul":
            n_mul[w] *= v
        elif field == "n_add":
            n_add[w] += v
        elif field == "n_extra":                 # 這段時間平均多 v 次（隨機落在各 30 秒）
            extra[w] += v / w.sum()
        elif field == "lat_mul":
            lat_mul[w] *= v
        elif field == "lat_fixed":
            lat_fixed[w] = v
        elif field == "err_n":
            err_n[w] = v
        elif field == "err_frac":
            err_frac[w] = v
    if n is None:
        n = rng.poisson(lam * n_mul + extra).astype(float) + n_add
    else:
        n = np.round(n * n_mul) + n_add + rng.poisson(extra)
    keep = ~in_outage(ts)
    errs = np.where(err_n >= 0, np.minimum(err_n, n), rng.binomial(n.astype(int), spec.get("err", 0.0)))
    errs = np.where(np.isnan(err_frac), errs, np.round(n * np.nan_to_num(err_frac)))
    return {"ts": ts[keep], "n": n[keep].astype(int), "err": errs[keep].astype(int), "lat_mul": lat_mul[keep],
            "lat_fixed": lat_fixed[keep]}


def hist_points(d: dict, mean_lat: float, route: str, rng, res: dict, names_hash: str) -> list[tuple[str, dict]]:
    """每 30 秒、每個狀態碼一個直方圖點（沒有請求的不送：SDK 的 delta 只送有更新的點）→ (_id, 文件)。"""
    out = []
    for status, counts in ((200, d["n"] - d["err"]), (500, d["err"])):
        idx = np.flatnonzero(counts > 0)
        if idx.size == 0:
            continue
        reps = counts[idx]
        owner = np.repeat(np.arange(idx.size), reps)
        lat = rng.gamma(2.0, mean_lat / 2.0, owner.size) * np.repeat(d["lat_mul"][idx], reps)
        fixed = np.repeat(d["lat_fixed"][idx], reps)
        lat = np.where(np.isnan(fixed), lat, fixed)
        bucket = np.searchsorted(np.asarray(O.HTTP_BOUNDS), lat, side="left")
        nb = len(O.HTTP_BOUNDS) + 1
        grid = np.bincount(owner * nb + bucket, minlength=idx.size * nb).reshape(idx.size, nb)
        sums = np.bincount(owner, weights=lat, minlength=idx.size)
        mins = np.full(idx.size, np.inf)
        maxs = np.zeros(idx.size)
        np.minimum.at(mins, owner, lat)
        np.maximum.at(maxs, owner, lat)
        attrs = {"http.request.method": "GET", "http.route": route, "http.response.status_code": status}
        for j, i in enumerate(idx):
            t = d["ts"][i].value
            out.append((f"{res['service.name']}|{status}|{t}",
                        O.metric_doc(res, attrs, t - STEP.value, t, grid[j].tolist(), float(sums[j]), float(mins[j]), float(maxs[j]), names_hash)))
    return out


def resource(svc: str, dataset: str) -> dict:
    return {"service.name": svc, "k8s.namespace.name": "it-apps", "k8s.pod.name": f"{svc}-7d9f8-x1",
            "k8s.deployment.name": svc, "dept": "D3120", "group": "it", "k8s.cluster.name": BY,
            "data_stream.dataset": dataset, "data_stream.namespace": "it"}


def count_docs(es: ES, index: str, field: str, value: str) -> int:
    r = es.req("POST", f"/{index}/_count?ignore_unavailable=true&allow_no_indices=true", {"query": {"term": {field: value}}}, ok=(200, 404))
    return int(r.get("count", 0))


def write_metrics(es: ES, services: dict, inject: list, dataset: str, seed: int, names_hash: str) -> int:
    rng = np.random.default_rng(seed)
    total = 0
    index = f"metrics-{dataset}.otel-it"
    for svc, spec in services.items():
        d = make_service(svc, spec, inject, rng)            # 每個服務都產生（亂數序列才會跟上一次一樣），寫過的跳過
        docs = hist_points(d, spec.get("lat", 0.08), spec["route"], rng, resource(svc, dataset), names_hash)
        have = count_docs(es, index, "resource.attributes.service.name", svc)
        if have != len(docs):
            O.bulk(es, index, docs)
        total += len(docs)
        print(f"  {svc}：{len(docs)} 個點（{spec['start']:%m-%d} 起）{'，已經寫過' if have == len(docs) else ''}", flush=True)
    return total


def write_logs(es: ES, seed: int) -> int:
    """svc-logs：info 每分鐘約 1 筆、error 每小時約 1.2 筆（只有它有 error log）；10-26 20:00 那小時多 15 筆 error。"""
    rng = np.random.default_rng(seed)
    ts = pd.date_range(D0, DATA_END, freq=STEP, inclusive="left")
    ts = ts[~in_outage(ts)]
    info = rng.poisson(0.5, len(ts))
    err = rng.poisson(1.2 / 120, len(ts))
    burst = (ts >= at(LOG_BURST[0])) & (ts < at(LOG_BURST[0]) + pd.Timedelta("1h"))
    err[np.flatnonzero(burst)[rng.integers(0, burst.sum(), LOG_BURST[1])]] += 1
    res = {"service.name": "svc-logs", "k8s.namespace.name": "it-apps", "k8s.pod.name": "svc-logs-6c5d-x1",
           "k8s.pod.uid": "uid-svc-logs", "k8s.container.name": "app", "k8s.node.name": "node-1",
           "k8s.deployment.name": "svc-logs", "dept": "D3120", "group": "it", "k8s.cluster.name": BY,
           "data_stream.namespace": "it", "k8s.pod.labels.app.kubernetes.io/name": "svc-logs"}
    recs = []
    for t, ni, ne in zip(ts, info, err):
        base = t.value
        recs += [O.log_record(base + k * 1_000_000, "INFO", f"handled request {k}") for k in range(ni)]
        recs += [O.log_record(base + (ni + k) * 1_000_000, "ERROR", f"db timeout {k}") for k in range(ne)]
    have = count_docs(es, LOG_INDEX, "resource.attributes.service.name", "svc-logs")
    if have != len(recs):
        for i in range(0, len(recs), 2000):
            O.post_retry(es, "/_otlp/v1/logs", O.logs_request([(res, recs[i:i + 2000])]))
    print(f"  svc-logs：{len(recs)} 筆 log（error {int(err.sum())} 筆）{'，已經寫過' if have == len(recs) else ''}", flush=True)
    return len(recs)


def setup(es: ES, fresh: bool):
    """metrics 的 data stream 用 standard mode（同一組 component template，TSDS 寫不進 7 天前的資料）。fresh：清掉上一次的資料。"""
    es.req("PUT", "/_index_template/bedge-metrics", {
        "index_patterns": [f"metrics-{EDGE_DS}*.otel-*", f"metrics-{TUNE_DS}*.otel-*"], "priority": 300, "data_stream": {},
        "composed_of": ["otel@mappings", "otel@settings", "metrics-otel@mappings", "semconv-resource-to-ecs@mappings",
                        "ecs-tsdb@mappings"],
        "template": {"settings": {"index": {"mode": "standard", "number_of_replicas": 0}},
                     "mappings": {"properties": {"data_stream.type": {"type": "constant_keyword", "value": "metrics"}}}},
        "_meta": {"description": "baseline_edge_e2e：寫得進 4 週前的資料"}})
    for ds in (f"metrics-{EDGE_DS}.otel-it", f"metrics-{TUNE_DS}.otel-it", LOG_INDEX, "logs-generic.otel-it") if fresh else ():
        es.req("DELETE", f"/_data_stream/{ds}", ok=(200, 404))


def clean_results(es: ES, prefix: str, anomalies_only: bool = False):
    for idx in ((ANOMALY_INDEX,) if anomalies_only else (BASELINE_INDEX, ANOMALY_INDEX)):
        es.req("POST", f"/{idx}/_delete_by_query?refresh=true&ignore_unavailable=true&conflicts=proceed",
               {"query": {"prefix": {"series": prefix}}}, ok=(200, 404))


def clean_tuning(es: ES):
    es.req("POST", f"/{TUNING_INDEX}/_delete_by_query?refresh=true&ignore_unavailable=true&conflicts=proceed",
           {"query": {"term": {"by": BY}}}, ok=(200, 404))


# ── 設定：從 series.yaml 複製 ──

def make_config(prefix: str, dataset: str, names=("http.requests", "http.error_rate", "http.latency_p95_s", "log.lines", "log.errors"),
                extra: dict | None = None) -> str:
    with open("configs/baseline/series.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    series = []
    for s in cfg["series"]:
        if s["name"] not in names:
            continue
        s = dict(s, name=f"{prefix}.{s['name']}")
        s["index"] = f"metrics-{dataset}.otel-*" if s["index"].startswith("metrics-") else LOG_INDEX
        series.append(s)
    cfg["series"] = series
    cfg["adjustments_file"] = ""       # 只用 aiops-tuning＋這裡的 overrides，不讀正式的調整、行事曆
    cfg["calendar_file"] = ""
    cfg.update(extra or {})
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix=f"bedge-{prefix}-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
    return path


def run(argv: list[str], quiet: bool = True) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli(argv)
    out = buf.getvalue()
    if not quiet:
        print(out)
    if rc:
        raise RuntimeError(f"{' '.join(argv)} → {rc}\n{out}")
    return out


def train(config: str, end: pd.Timestamp, quiet=True) -> str:
    return run(["train", "--config", config, "--end", end.tz_convert("UTC").isoformat()], quiet)


def detect(config: str, at_: pd.Timestamp, window: str = "1D", quiet=True) -> str:
    return run(["detect", "--config", config, "--window", window, "--at", at_.tz_convert("UTC").isoformat()], quiet)


def anomalies(es: ES, prefix: str, start=None, end=None) -> pd.DataFrame:
    es.req("POST", f"/{ANOMALY_INDEX}/_refresh")
    flt = [{"prefix": {"series": prefix}}]
    if start is not None:
        flt.append({"range": {"@timestamp": {"gte": start.isoformat(), "lt": end.isoformat()}}})
    rows, after = [], None
    while True:
        body = {"size": 5000, "query": {"bool": {"filter": flt}}, "sort": [{"@timestamp": "asc"}, {"_doc": "asc"}]}
        if after:
            body["search_after"] = after
        hits = es.req("POST", f"/{ANOMALY_INDEX}/_search", body)["hits"]["hits"]
        for h in hits:
            s = h["_source"]
            k = s["keys"]
            rows.append({"series": s["series"].split(".", 1)[1], "svc": k.get("service") or k.get("resource.attributes.service.name"),
                         "ts": pd.Timestamp(s["@timestamp"]).tz_convert(TZ), "status": s.get("status"), "z": s.get("z"),
                         "level": s.get("level"), "resolution": s.get("resolution"), "method": s.get("method"),
                         "confidence": s.get("confidence"), "value": s.get("value"), "median": s.get("median")})
        if len(hits) < 5000:
            break
        after = hits[-1]["sort"]
    return pd.DataFrame(rows, columns=["series", "svc", "ts", "status", "z", "level", "resolution", "method", "confidence",
                                       "value", "median"])


def baseline_rows(es: ES, series: str, svc: str) -> pd.DataFrame:
    es.req("POST", f"/{BASELINE_INDEX}/_refresh")
    body = {"size": 1000, "query": {"bool": {"filter": [{"term": {"series": series}}], "minimum_should_match": 1, "should": [
        {"term": {"keys.service": svc}}, {"term": {"keys.resource.attributes.service.name": svc}}]}}}
    hits = es.req("POST", f"/{BASELINE_INDEX}/_search", body)["hits"]["hits"]
    return pd.DataFrame([h["_source"] for h in hits])


def slot(b: pd.DataFrame, dow: int, hour: int) -> dict:
    if b.empty:
        return {}
    r = b[(b["dow"] == dow) & (b["hour"] == hour)]
    return r.iloc[0].to_dict() if not r.empty else {}


def scored_points(config: str, es: ES, now: pd.Timestamp, window: str, services: list[str]) -> pd.DataFrame:
    """跟 cmd_detect 同一個函式（detect_series）、不寫 ES：每個被判斷的點（分母）和它的 status。"""
    with open(config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    tz = cfg["train"]["timezone"]
    dopts = DetectOptions(**cfg.get("detect", {}))
    overrides, adjs = load_overrides(es, cfg, now), data_adjustments(es, cfg, now)
    out = []
    for spec in cfg["series"]:
        keys = keys_of(spec)
        base = load_baseline(es, spec["name"], keys)
        if base.empty:
            continue
        for r, s in detect_series(es, cfg, spec, base, now, pd.Timedelta(window), dopts, tz, overrides, adjs, quiet=True):
            if s.empty:
                continue
            svc = s[keys[0]].astype(str)
            s = s[svc.isin(services)]
            out.append(pd.DataFrame({"series": spec["name"].split(".", 1)[1], "svc": s[keys[0]].astype(str), "ts": s["ts"],
                                     "status": s["status"], "resolution": r}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["series", "svc", "ts", "status", "resolution"])


def old_fill_lambda(es: ES, config: str) -> float:
    """修正前的 _fill_zero（alive 只看這條序列自己查到的資料）學出來的 svc-logs log.errors λ：沒有任何 error 的小時變成「不知道」。"""
    import detector.es as DE
    from detector.model import TrainOptions, train as mtrain
    with open(config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    spec = next(s for s in cfg["series"] if s["name"].endswith("log.errors"))
    orig = DE.alive_times
    DE.alive_times = lambda *a, **k: None
    try:
        h = DE.fetch(es, spec, T_END - pd.Timedelta(weeks=4), T_END, "1h")
    finally:
        DE.alive_times = orig
    t = mtrain(h, keys_of(spec), TrainOptions(**cfg["train"]), method="poisson")
    return float(t["median"].median()) if not t.empty else float("nan")


# ── 檢查 ──

RESULTS: list[tuple[str, str, str, str, bool]] = []   # (群組, 情境, 預期, 實際, 通過)


def check(group: str, name: str, expect: str, actual: str, ok: bool):
    RESULTS.append((group, name, expect, actual, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  [{group}] {name}：預期 {expect}；實際 {actual}", flush=True)


def statuses(df: pd.DataFrame, series: str, svc: str, start: str, end: str) -> list:
    w = df[(df["series"] == series) & (df["svc"] == svc) & (df["ts"] >= at(start)) & (df["ts"] < at(end))]
    return [(t.strftime("%H:%M:%S"), s) for t, s in zip(w["ts"], w["status"])]


def outside(df: pd.DataFrame, series: str, svc: str, windows: list[tuple[str, str]]) -> pd.DataFrame:
    w = df[(df["series"] == series) & (df["svc"] == svc)]
    for a, b in windows:
        w = w[~((w["ts"] >= at(a)) & (w["ts"] < at(b)))]
    return w


def fmt_counts(w: pd.DataFrame) -> str:
    return f"anomaly {int((w['status'] == 'anomaly').sum())}、warning {int((w['status'] == 'warning').sum())}"


# ── 誤報率 ──

def phase_fp(es: ES, config: str):
    print("\n==== 誤報率：10-19～10-26 每天 train、detect 隔天一整天（沒有注入異常的服務）", flush=True)
    clean_results(es, "edge.")
    frames = []
    for d in pd.date_range(FP_START, T_END, freq="D", inclusive="left"):
        train(config, d)
        detect(config, d + pd.Timedelta(days=1))
        frames.append(scored_points(config, es, d + pd.Timedelta(days=1), "1D", FP_SERVICES))
        print(f"  {d:%m-%d} 完成", flush=True)
    pts = pd.concat(frames, ignore_index=True)
    pts = pts[pts["ts"] >= FP_START.tz_convert("UTC")]
    got = anomalies(es, "edge.", FP_START, T_END)
    got = got[got["svc"].isin(FP_SERVICES)]
    rows = []
    for (series, svc), g in pts.groupby(["series", "svc"]):
        a = got[(got["series"] == series) & (got["svc"] == svc)]
        rows.append({"series": series, "svc": svc, "points": len(g), "warning": int((a["status"] == "warning").sum()),
                     "anomaly": int((a["status"] == "anomaly").sum()), "resolution": ",".join(sorted(g["resolution"].unique()))})
    tab = pd.DataFrame(rows)
    print(tab.to_string(index=False))
    n, w, an = tab["points"].sum(), tab["warning"].sum(), tab["anomaly"].sum()
    print(f"\n  合計：{n} 個點，warning {w}（{w / n:.3%}）、anomaly {an}（{an / n:.3%}）")
    ex = tab[tab["svc"] != "svc-peak"]
    print(f"  不含 svc-peak（尖峰不對齊整點）：{ex['points'].sum()} 個點，warning {ex['warning'].sum()}、anomaly {ex['anomaly'].sum()}"
          f"（{ex['anomaly'].sum() / ex['points'].sum():.3%}）")
    for (series, svc), a in got.groupby(["series", "svc"]):
        top = a.sort_values("ts").head(4)
        print(f"    {series} {svc}：" + "；".join(f"{t:%m-%d %H:%M:%S} {s} z={z:.1f}" for t, s, z in zip(top["ts"], top["status"], top["z"])))
    check("誤報率", "1 週 anomaly 率（全部）", "≤ 0.1%", f"{an}/{n}＝{an / n:.3%}", an / n <= 0.001)
    check("誤報率", "1 週 warning 率（全部）", "≤ 1%", f"{w}/{n}＝{w / n:.3%}", w / n <= 0.01)
    return tab, got


# ── 邊緣情境 ──

def phase_edge(es: ES, config: str):
    print("\n==== 邊緣情境：train 到 10-26 00:00、detect 10-26 一整天", flush=True)
    clean_results(es, "edge.")
    print(train(config, T_END))
    print(detect(config, DATA_END))
    df = anomalies(es, "edge.", T_END, DATA_END)
    G = "邊緣"

    # 時區、工作日／週末、上班尖峰、跨午夜：直接看 aiops-baseline 學出來的
    b = baseline_rows(es, "edge.http.requests", "svc-season")
    res = b["resolution"].iloc[0] if not b.empty else None
    k = pd.Timedelta(str(res).replace("m", "min")) / pd.Timedelta("1min") if res else 1   # 下面的數字是每分鐘
    m = {key: slot(b, *key).get("median") for key in [(0, 9), (5, 9), (0, 10), (5, 10), (0, 0), (6, 23), (0, 12)]}
    lv = {key: slot(b, *key).get("level") for key in m}
    check(G, "台灣時區：週一 9 點（尖峰）的平常", f"{120 * k:g}（1.5 倍；UTC 的話是 {80 * k:g}）",
          f"{m[(0, 9)]}（{lv[(0, 9)]}，{res} 一點）", m[(0, 9)] is not None and abs(m[(0, 9)] - 120 * k) <= 12 * k)
    check(G, "週末：週六 10 點／週一 10 點", "0.5", f"{m[(5, 10)]}／{m[(0, 10)]}",
          bool(m[(5, 10)] and m[(0, 10)]) and abs(m[(5, 10)] / m[(0, 10)] - 0.5) <= 0.07)
    check(G, "跨午夜：週一 0 點（批次）／週日 23 點", f"{96 * k:g}／{48 * k:g}", f"{m[(0, 0)]}／{m[(6, 23)]}",
          bool(m[(0, 0)] and m[(6, 23)]) and abs(m[(0, 0)] - 96 * k) <= 10 * k and abs(m[(6, 23)] - 48 * k) <= 6 * k)
    check(G, "收集中斷（歷史 10-12 10～16 點）不當 0：週一 12 點", f"約 {80 * k:g}、退到 hour（只剩 1 個週一）",
          f"{m[(0, 12)]}（{lv[(0, 12)]}）", bool(m[(0, 12)]) and m[(0, 12)] >= 70 * k and lv[(0, 12)] == "hour")
    w = outside(df, "http.requests", "svc-season", [("11:00", "11:12")])
    check(G, "季節性的服務：平常的一天（尖峰、跨午夜、週一凌晨）", "requests 0 個異常", fmt_counts(w), w.empty)
    st = statuses(df, "http.requests", "svc-season", "11:00", "11:12")
    check(G, "白天流量掉到一成（11:00～11:10）", "warning 後 anomaly",
          f"{len(st)} 點：{[s for _, s in st][:3]}…", len(st) >= 8 and st[0][1] == "warning" and all(s == "anomaly" for _, s in st[1:10]))
    w = df[(df["svc"] == "svc-peak") & (df["series"] == "http.requests")]
    check(G, "上班尖峰不對齊整點（08:45 起）", "0 個異常",
          f"{fmt_counts(w)}（{', '.join(sorted({t.strftime('%H:%M') for t in w['ts']})[:3])}…）", w.empty)

    # 剛上線：退層
    for svc, lvl in NEW.items():
        bl = baseline_rows(es, "edge.http.latency_p95_s", svc)
        br = baseline_rows(es, "edge.http.requests", svc)
        levels = sorted(set(br["level"])) if not br.empty else []
        llev = bl["level"].value_counts().to_dict() if not bl.empty else {}
        ok_level = levels == [lvl] if svc != "svc-new2w" else levels == ["dow_hour", "hour"]
        check(G, f"剛上線 {svc[7:]}：requests 的 baseline 層級", lvl + ("（10～15 點 hour：10-12 收集中斷）" if svc == "svc-new2w" else ""),
              f"requests {br['level'].value_counts().to_dict() if not br.empty else {}}；latency {llev}", ok_level)
        st = statuses(df, "http.latency_p95_s", svc, "14:00", "14:10")
        check(G, f"剛上線 {svc[7:]}：延遲 5 倍（14:00～14:10）", "抓到（anomaly）",
              f"{sum(s == 'anomaly' for _, s in st)} 個 anomaly", any(s == "anomaly" for _, s in st))
        w = outside(df, "http.requests", svc, [])
        check(G, f"剛上線 {svc[7:]}：requests 平常的一天", "0 個異常", fmt_counts(w), w.empty)

    # MAD = 0
    for series in ("http.requests", "http.latency_p95_s", "http.error_rate"):
        bf = baseline_rows(es, f"edge.{series}", "svc-flat")
        mads = sorted(set(bf["mad"].round(6))) if not bf.empty else []
        check(G, f"MAD＝0：{series} 學出來的 MAD", "全部 0", f"{mads[:3]}", mads == [0.0])
    check(G, "MAD＝0：請求數 +10%（10:00）", "不報（z 2）", str(statuses(df, "http.requests", "svc-flat", "10:00", "10:06")),
          not statuses(df, "http.requests", "svc-flat", "10:00", "10:06"))
    st = statuses(df, "http.requests", "svc-flat", "11:00", "11:06")
    check(G, "MAD＝0：請求數 +40%（11:00～11:05）", "報（z 8）", f"{len(st)} 點 {[s for _, s in st][:2]}", any(s == "anomaly" for _, s in st))
    st = statuses(df, "http.latency_p95_s", "svc-flat", "14:00", "14:06")
    check(G, "MAD＝0：延遲換一格（14:00～14:05）", "報", f"{len(st)} 點 {[s for _, s in st][:2]}", any(s == "anomaly" for _, s in st))
    check(G, "MAD＝0、平常 0：錯誤率 1/30（13:00）", "不報（< min_abs 5%）",
          str(statuses(df, "http.error_rate", "svc-flat", "13:00", "13:01")), not statuses(df, "http.error_rate", "svc-flat", "13:00", "13:01"))
    st = statuses(df, "http.error_rate", "svc-flat", "13:30", "13:31")
    check(G, "MAD＝0、平常 0：錯誤率 33%（13:30）", "anomaly（confirm_points 1）", str(st), [s for _, s in st] == ["anomaly"])
    w = outside(df, "http.requests", "svc-flat", [("11:00", "11:06")])
    w = pd.concat([w, outside(df, "http.latency_p95_s", "svc-flat", [("14:00", "14:06")]),
                   outside(df, "http.error_rate", "svc-flat", [("13:30", "13:31")])])
    check(G, "MAD＝0：其他時間", "0 個異常", fmt_counts(w), w.empty)

    # 稀疏計數：卜瓦松
    br = baseline_rows(es, "edge.http.requests", "svc-rare")
    meth = sorted(set(br["method"])) if not br.empty else []
    lam = float(br["median"].median()) if not br.empty else float("nan")
    check(G, "稀疏（每 5 分鐘 0～2 筆）：requests 用什麼學", "poisson、1h、λ≈6",
          f"{meth}、{sorted(set(br['resolution'])) if not br.empty else []}、λ 中位數 {lam:.2f}、層級 {sorted(set(br['level'])) if not br.empty else []}",
          meth == ["poisson"] and 4.8 <= lam <= 7.2)
    st = statuses(df, "http.requests", "svc-rare", "14:00", "15:00")
    check(G, "稀疏：14 點多 24 次", "anomaly", str(st), [s for _, s in st] == ["anomaly"])
    st = statuses(df, "http.requests", "svc-rare", "00:00", "24:00")
    st = [x for x in st if x[0] != "14:00:00"]
    check(G, "稀疏：其他小時（含 10 點多 6 次）", "0 個異常", str(st), not st)
    bl = baseline_rows(es, "edge.http.latency_p95_s", "svc-rare")
    check(G, "稀疏：延遲（比例類）", "沒有自己的 baseline、不判斷", f"{len(bl)} 個時段",
          bl.empty and df[(df["svc"] == "svc-rare") & (df["series"] != "http.requests")].empty)
    bl = baseline_rows(es, "edge.log.errors", "svc-logs")
    lam = float(bl["median"].median()) if not bl.empty else float("nan")
    check(G, "稀疏 log（每小時約 1.2 筆 error，只有這個服務有）：λ", "poisson、λ≈1.2",
          f"{sorted(set(bl['method'])) if not bl.empty else []}、{sorted(set(bl['resolution'])) if not bl.empty else []}、λ 中位數 {lam:.2f}",
          not bl.empty and set(bl["method"]) == {"poisson"} and 0.9 <= lam <= 1.5)
    lam_old = old_fill_lambda(es, config)
    print(f"  對照（修正前的 _fill_zero：只看 error log 自己判斷收集系統有沒有在跑）：λ 中位數 {lam_old:.2f}")
    st = statuses(df, "log.errors", "svc-logs", "20:00", "21:00")
    check(G, "稀疏 log：20 點多 15 筆 error", "anomaly", str(st), [s for _, s in st] == ["anomaly"])
    w = outside(df, "log.errors", "svc-logs", [("20:00", "21:00")])
    w = pd.concat([w, df[(df["svc"] == "svc-logs") & (df["series"] == "log.lines")]])
    check(G, "稀疏 log：其他時間", "0 個異常", fmt_counts(w), w.empty)

    # 收集中斷
    w = df[(df["ts"] >= at("15:00")) & (df["ts"] < at("16:00"))]
    check(G, "收集中斷（10-26 15～16 點）：detect 一整天", "中斷時段 0 個異常", fmt_counts(w), w.empty)
    outage = {}
    for t in ("15:40", "16:10"):
        clean_results(es, "edge.", anomalies_only=True)
        detect(config, at(t), "30min")
        a = anomalies(es, "edge.", T_END, DATA_END)
        outage[t] = a
        check(G, f"收集中斷：像正式環境一樣 detect（{t}、看 30 分鐘）", "0 個異常",
              f"{fmt_counts(a)}（{'；'.join(f'{x} {y} {z:%H:%M}' for x, y, z in zip(a['svc'], a['series'], a['ts']))[:120]}）", a.empty)
        # 中斷造成的：時間點在中斷時段內（含補 0 的「服務停掉」）
        w = a[(a["ts"] >= at("15:00")) & (a["ts"] < at("16:00"))]
        check(G, f"收集中斷：像正式環境一樣 detect（{t}）、中斷時段內的異常", "0 個", fmt_counts(w), w.empty)
    # 對照：修正前的 _add_silent（不看收集系統有沒有在跑，有 baseline 但整段沒資料的一律補 0）
    import detector.cmd_baseline as CB
    orig = CB._add_silent
    CB._add_silent = lambda cur, base, by, s_, e_, i_, alive=None: orig(cur, base, by, s_, e_, i_)
    try:
        old = scored_points(config, es, at("15:40"), "30min", list(SERVICES) + ["svc-logs"])
    finally:
        CB._add_silent = orig
    old = old[old["status"].notna()]
    print(f"  對照（修正前的 _add_silent，15:40、30 分鐘）：{fmt_counts(old)}，{old['svc'].nunique()} 個服務："
          + "、".join(f"{k} {v}" for k, v in old.groupby("series").size().items()))

    # 漂移、單點、連續
    w = df[(df["svc"] == "svc-spike") & (df["series"] == "http.latency_p95_s")]
    check(G, "緩慢漂移（延遲每天 +1%）", "0 個異常（也抓不到漂移本身）", fmt_counts(w), w.empty)
    sp = lambda a, b: [s for _, s in statuses(df, "http.requests", "svc-spike", a, b)]
    check(G, "單點尖峰 z 約 8（10:00）", "warning", str(sp("10:00", "10:01")), sp("10:00", "10:01") == ["warning"])
    zz = df[(df["svc"] == "svc-spike") & (df["ts"] == at("10:30"))]["z"].tolist()
    check(G, "單點尖峰 z ≥ 15（10:30）", "anomaly（instant_z）", f"{sp('10:30', '10:31')} z={zz}", sp("10:30", "10:31") == ["anomaly"])
    check(G, "連續兩點（11:00、11:01）", "warning、anomaly", str(sp("11:00", "11:02")), sp("11:00", "11:02") == ["warning", "anomaly"])
    check(G, "隔一點的兩點（11:30、11:32）", "warning、warning", str(sp("11:30", "11:33")), sp("11:30", "11:33") == ["warning", "warning"])
    st = [s for _, s in statuses(df, "http.error_rate", "svc-spike", "12:30", "12:31")]
    check(G, "錯誤率單點 50%（12:30）", "anomaly", str(st), st == ["anomaly"])
    w = outside(df, "http.requests", "svc-spike", [("10:00", "10:01"), ("10:30", "10:31"), ("11:00", "11:02"), ("11:30", "11:33")])
    w = pd.concat([w, outside(df, "http.error_rate", "svc-spike", [("12:30", "12:31")]), outside(df, "http.latency_p95_s", "svc-spike", [])])
    check(G, "svc-spike 其他時間", "0 個異常", fmt_counts(w), w.empty)

    # 階梯式永久改變（換版後延遲 2 倍），沒有 reset_point
    step = df[(df["svc"] == "svc-step") & (df["series"] == "http.latency_p95_s")]
    n_an = int((step["status"] == "anomaly").sum())
    check(G, "階梯式改變（4 天前延遲 2 倍）、沒有 reset_point", "整天報（README 的已知限制）",
          f"anomaly {n_an} 個 30 秒點", n_an >= 2000)
    return df


def phase_reset(es: ES, config: str):
    """reset_point：proposed 不生效 → approve 後、還沒重新 train（baseline 是舊的）不報 → 重新 train 後用新水準判斷。"""
    print("\n==== 調整介面：reset_point（svc-step）", flush=True)
    G = "調整"
    count = lambda: anomalies(es, "edge.", T_END, DATA_END).query("svc == 'svc-step' and series == 'http.latency_p95_s'")
    run(["adjust", "add", "--config", config, "--kind", "reset_point", "--match", "service=^svc-step$", "--at", STEP_AT.isoformat(),
         "--status", "proposed", "--source", "cicd", "--id", "bedge-step-v2", "--reason", "v2 上線", "--by", BY])
    clean_results(es, "edge.", anomalies_only=True)
    detect(config, DATA_END)
    a = count()
    check(G, "reset_point status=proposed", "不生效（照樣整天報）", fmt_counts(a), (a["status"] == "anomaly").sum() >= 2000)
    run(["adjust", "approve", "--config", config, "--id", "bedge-step-v2", "--by", BY])
    clean_results(es, "edge.", anomalies_only=True)
    detect(config, DATA_END)
    a = count()
    check(G, "approve 後、下一輪 detect（還沒重新 train）", "0 個（baseline 是重置前學的，不判斷）", fmt_counts(a), a.empty)
    out = train(config, T_END)
    line = next((l for l in out.splitlines() if "排除" in l), "")
    b = baseline_rows(es, "edge.http.latency_p95_s", "svc-step")
    clean_results(es, "edge.", anomalies_only=True)
    detect(config, DATA_END)
    a = count()
    st = [s for _, s in statuses(a, "http.latency_p95_s", "svc-step", "16:30", "16:36")]
    check(G, "下一輪 train：重置點之前不學", "層級 hour（重置後 3.5 天）",
          f"{line.strip()[:40]}；層級 {sorted(set(b['level']))}", set(b["level"]) == {"hour"})
    check(G, "重新 train 後 detect", "只報 16:30 的新異常", f"16:30～16:35 {st}；其他 {fmt_counts(a[(a['ts'] < at('16:30')) | (a['ts'] >= at('16:36'))])}",
          "anomaly" in st and a[(a["ts"] < at("16:30")) | (a["ts"] >= at("16:36"))].empty)
    run(["adjust", "remove", "--config", config, "--id", "bedge-step-v2"])


# ── learning loop 的調整介面（tune 資料集） ──

def put_tuning(es: ES, _id: str, doc: dict):
    es.req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", {"by": BY, "at": pd.Timestamp.now(tz="UTC").isoformat(), **doc})


def phase_tune(es: ES):
    print("\n==== 調整介面：tune、override、exclude_window、recurring_override（svc-ta 調、svc-tb 對照）", flush=True)
    G = "調整"
    config = make_config("tune", TUNE_DS, names=("http.requests", "http.latency_p95_s"))
    clean_results(es, "tune.")
    clean_tuning(es)
    at_, win = at("12:00"), "3h"     # 看 09:00～12:00：10:00 延遲 3 倍（5 分鐘）、11:00 請求數單點 +70
    train(config, T_END)

    def look(cfg=config) -> pd.DataFrame:
        clean_results(es, "tune.", anomalies_only=True)
        detect(cfg, at_, win)
        return anomalies(es, "tune.", T_END, DATA_END)

    def req(a, svc):
        return [s for _, s in statuses(a, "http.requests", svc, "11:00", "11:01")]

    def lat(a, svc):
        return [s for _, s in statuses(a, "http.latency_p95_s", svc, "10:00", "10:06")]

    a0 = look()
    check(G, "調整前（對照）", "兩個服務：11:00 請求數 warning、10:00 延遲 anomaly",
          f"ta {req(a0, 'svc-ta')} {lat(a0, 'svc-ta')[:2]}；tb {req(a0, 'svc-tb')} {lat(a0, 'svc-tb')[:2]}",
          req(a0, "svc-ta") == ["warning"] == req(a0, "svc-tb") and "anomaly" in lat(a0, "svc-ta") and "anomaly" in lat(a0, "svc-tb"))

    def tune(*args):
        run(["tune", "--config", config, "--series", "tune\\.http\\.requests", "--match", "service=^svc-ta$", *args,
             "--reason", "bedge", "--by", BY])

    def untune():
        run(["tune", "--config", config, "--series", "tune\\.http\\.requests", "--match", "service=^svc-ta$", "--remove"])

    tune("--z", "50")
    a = look()
    check(G, "override z=50（tune）→ 下一輪 detect", "ta 不報、tb 照報", f"ta {req(a, 'svc-ta')}；tb {req(a, 'svc-tb')}",
          req(a, "svc-ta") == [] and req(a, "svc-tb") == ["warning"])
    untune()
    tune("--min-abs", "1000")
    a = look()
    check(G, "override min_abs=1000", "ta 不報", f"ta {req(a, 'svc-ta')}；tb {req(a, 'svc-tb')}", req(a, "svc-ta") == [] and req(a, "svc-tb") == ["warning"])
    untune()
    tune("--confirm-points", "1")
    a = look()
    check(G, "override confirm_points=1", "ta 單點就 anomaly", f"ta {req(a, 'svc-ta')}；tb {req(a, 'svc-tb')}",
          req(a, "svc-ta") == ["anomaly"] and req(a, "svc-tb") == ["warning"])
    untune()
    tune("--instant-z", "6")
    a = look()
    zt = a[(a["svc"] == "svc-ta") & (a["ts"] == at("11:00")) & (a["series"] == "http.requests")]["z"].tolist()
    check(G, "override instant_z=6", "ta 單點就 anomaly", f"ta {req(a, 'svc-ta')}（z={zt}）；tb {req(a, 'svc-tb')}",
          req(a, "svc-ta") == ["anomaly"] and req(a, "svc-tb") == ["warning"])
    untune()
    run(["tune", "--config", config, "--series", "tune\\.http\\.latency_p95_s", "--match", "service=^svc-ta$", "--resolution", "5m",
         "--reason", "bedge", "--by", BY])
    a = look()
    r_before = sorted(set(a[(a["svc"] == "svc-ta") & (a["series"] == "http.latency_p95_s")]["resolution"]))
    train(config, T_END)
    ra = sorted(set(baseline_rows(es, "tune.http.latency_p95_s", "svc-ta")["resolution"]))
    rb = sorted(set(baseline_rows(es, "tune.http.latency_p95_s", "svc-tb")["resolution"]))
    a = look()
    st = statuses(a, "http.latency_p95_s", "svc-ta", "10:00", "10:06")
    check(G, "override resolution=5m", "detect 不變；下一輪 train 後 ta 5m、tb 照舊",
          f"train 前 detect {r_before}；train 後 ta {ra}、tb {rb}；ta 10:00 {st}",
          r_before == ["30s"] and ra == ["5m"] and rb == ["30s"] and any(s == "anomaly" for _, s in st))
    run(["tune", "--config", config, "--series", "tune\\.http\\.latency_p95_s", "--match", "service=^svc-ta$", "--remove"])
    train(config, T_END)

    # status、expires_at（learning loop 寫的 override）
    base = {"kind": "override", "series": "tune\\.http\\.requests", "match": {"service": "^svc-ta$"}, "z": 50, "source": "feedback"}
    put_tuning(es, "bedge-ov", {**base, "status": "proposed"})
    a = look()
    check(G, "override status=proposed", "不生效", f"ta {req(a, 'svc-ta')}", req(a, "svc-ta") == ["warning"])
    put_tuning(es, "bedge-ov", {**base, "status": "rejected"})
    a = look()
    check(G, "override status=rejected（不是 active）", "不生效", f"ta {req(a, 'svc-ta')}", req(a, "svc-ta") == ["warning"])
    past = (at_ - pd.Timedelta(hours=1)).tz_convert("UTC").isoformat()     # 過期：以 --at 判斷（回放）
    put_tuning(es, "bedge-ov", {**base, "status": "active", "expires_at": past})
    a = look()
    check(G, "override expires_at 已過", "不生效", f"ta {req(a, 'svc-ta')}", req(a, "svc-ta") == ["warning"])
    put_tuning(es, "bedge-ov", {**base, "status": "active", "expires_at": (pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=1)).isoformat()})
    a = look()
    check(G, "override expires_at 還沒到", "生效", f"ta {req(a, 'svc-ta')}", req(a, "svc-ta") == [])
    # detect --at 在過去（回放、backtest）：以 --at 判斷有沒有過期，跟排除時段、重置點、backtest 一樣
    put_tuning(es, "bedge-ov", {**base, "status": "active", "expires_at": DATA_END.tz_convert("UTC").isoformat()})
    a = look()
    check(G, "override expires_at 在 --at 之後、現在之前（回放）", "生效（跟 exclude_window 一樣以 --at 判斷）", f"ta {req(a, 'svc-ta')}", req(a, "svc-ta") == [])
    es.req("DELETE", f"/{TUNING_INDEX}/_doc/bedge-ov?refresh=true", ok=(200, 404))

    # series.yaml 的 overrides vs aiops-tuning
    yaml_ov = [{"series": "tune\\.http\\.requests", "match": {"service": "^svc-ta$"}, "z": 50}]
    cfg2 = make_config("tune", TUNE_DS, names=("http.requests", "http.latency_p95_s"), extra={"overrides": yaml_ov})
    a = look(cfg2)
    check(G, "series.yaml overrides z=50", "生效（ta 不報）", f"ta {req(a, 'svc-ta')}", req(a, "svc-ta") == [])
    tune("--z", "5")
    a = look(cfg2)
    check(G, "衝突：series.yaml z=50、aiops-tuning z=5", "aiops-tuning 優先（ta 照報）", f"ta {req(a, 'svc-ta')}", req(a, "svc-ta") == ["warning"])
    untune()
    tune("--confirm-points", "1")
    a = look(cfg2)
    check(G, "不同欄位：series.yaml z=50、aiops-tuning confirm_points=1", "各自生效（z=50 → 不報）", f"ta {req(a, 'svc-ta')}", req(a, "svc-ta") == [])
    untune()

    # exclude_window：不報（detect）、不學（train）
    run(["adjust", "add", "--config", config, "--kind", "exclude_window", "--series", "tune\\..*", "--match", "service=^svc-ta$",
         "--start", at("10:00").isoformat(), "--end", at("10:10").isoformat(), "--apply-to", "train,detect", "--id", "bedge-ex",
         "--reason", "維護", "--by", BY])
    a = look()
    check(G, "exclude_window apply_to detect", "ta 10:00 延遲不報、tb 照報", f"ta {lat(a, 'svc-ta')[:2]}；tb {lat(a, 'svc-tb')[:2]}",
          lat(a, "svc-ta") == [] and "anomaly" in lat(a, "svc-tb"))
    run(["adjust", "add", "--config", config, "--kind", "exclude_window", "--series", "tune\\..*", "--match", "service=^svc-ta$",
         "--start", at("10:00").isoformat(), "--end", at("10:10").isoformat(), "--apply-to", "train,detect", "--id", "bedge-ex",
         "--status", "proposed", "--reason", "維護", "--by", BY])
    a = look()
    check(G, "exclude_window status=proposed", "不生效", f"ta {lat(a, 'svc-ta')[:2]}", "anomaly" in lat(a, "svc-ta"))
    run(["adjust", "add", "--config", config, "--kind", "exclude_window", "--series", "tune\\..*", "--match", "service=^svc-ta$",
         "--start", at("10:00").isoformat(), "--end", at("10:10").isoformat(), "--apply-to", "train,detect", "--id", "bedge-ex",
         "--expires", (at_ - pd.Timedelta(hours=1)).tz_convert("UTC").isoformat(), "--reason", "維護", "--by", BY])
    a = look()
    check(G, "exclude_window expires_at 已過", "不生效", f"ta {lat(a, 'svc-ta')[:2]}", "anomaly" in lat(a, "svc-ta"))
    run(["adjust", "remove", "--config", config, "--id", "bedge-ex"])
    mondays = [(T_END - pd.Timedelta(weeks=w)) for w in (1, 2)]
    for i, d in enumerate(mondays):
        run(["adjust", "add", "--config", config, "--kind", "exclude_window", "--series", "tune\\..*", "--match", "service=^svc-ta$",
             "--start", d.isoformat(), "--end", (d + pd.Timedelta(days=1)).isoformat(), "--id", f"bedge-exmon{i}", "--reason", "測試", "--by", BY])
    a = look()
    lv_before = sorted(set(baseline_rows(es, "tune.http.latency_p95_s", "svc-ta").query("dow == 0")["level"]))
    train(config, T_END)
    lv_ta = sorted(set(baseline_rows(es, "tune.http.latency_p95_s", "svc-ta").query("dow == 0")["level"]))
    lv_tb = sorted(set(baseline_rows(es, "tune.http.latency_p95_s", "svc-tb").query("dow == 0")["level"]))
    check(G, "exclude_window apply_to train（排掉過去兩個週一）", "下一輪 train：ta 週一全部退到 hour、tb 不變",
          f"train 前 ta {lv_before}；train 後 ta {lv_ta}、tb {lv_tb}", "dow_hour" in lv_before and lv_ta == ["hour"] and lv_tb == lv_before)
    for i in range(2):
        run(["adjust", "remove", "--config", config, "--id", f"bedge-exmon{i}"])
    train(config, T_END)

    # recurring_override：週一 10:00 起 1 小時
    def rec(*args, id_="bedge-rec", cron="0 10 * * 1"):
        run(["adjust", "add", "--config", config, "--kind", "recurring_override", "--series", "tune\\..*", "--match", "service=^svc-ta$",
             "--cron", cron, "--duration", "1h", "--id", id_, *args, "--reason", "週一批次", "--by", BY])
    rec("--mute")
    a = look()
    check(G, "recurring_override mute（週一 10～11 點）", "ta 10:00 不報、11:00 照報；tb 照報",
          f"ta {lat(a, 'svc-ta')[:2]} {req(a, 'svc-ta')}；tb {lat(a, 'svc-tb')[:2]}",
          lat(a, "svc-ta") == [] and req(a, "svc-ta") == ["warning"] and "anomaly" in lat(a, "svc-tb"))
    rec("--z", "1000")
    a = look()
    check(G, "recurring_override z=1000", "ta 10:00 不報", f"ta {lat(a, 'svc-ta')[:2]}", lat(a, "svc-ta") == [])
    rec("--z", "2")
    a = look()
    n_ta = len(a[(a["svc"] == "svc-ta") & (a["ts"] >= at("10:00")) & (a["ts"] < at("11:00"))])
    n_ta0 = len(a0[(a0["svc"] == "svc-ta") & (a0["ts"] >= at("10:00")) & (a0["ts"] < at("11:00"))])
    check(G, "recurring_override z=2（比預設嚴）", "不變嚴（只會放寬）", f"10～11 點 ta {n_ta} 個（調整前 {n_ta0} 個）", n_ta == n_ta0)
    rec("--z", "1000", "--train", "exclude", cron="0 17 * * 1")     # 17 點：避開 10-12 收集中斷（10～16 點本來就是 hour）
    train(config, T_END)
    s10 = slot(baseline_rows(es, "tune.http.latency_p95_s", "svc-ta"), 0, 17).get("level")
    s11 = slot(baseline_rows(es, "tune.http.latency_p95_s", "svc-ta"), 0, 18).get("level")
    check(G, "recurring_override train=exclude（週一 17 點）", "下一輪 train：ta 週一 17 點不學、退到 hour；18 點照舊", f"週一 17 點 {s10}、18 點 {s11}",
          s10 == "hour" and s11 == "dow_hour")
    run(["adjust", "remove", "--config", config, "--id", "bedge-rec"])
    train(config, T_END)

    # 讀不到 aiops-tuning、內容壞掉
    tune("--z", "50")
    clean_results(es, "tune.", anomalies_only=True)
    es.req("POST", f"/{TUNING_INDEX}/_close")
    try:
        try:
            out = run(["detect", "--config", config, "--window", win, "--at", at_.tz_convert("UTC").isoformat()])
            a = anomalies(es, "tune.", T_END, DATA_END)
            note = next((l.strip() for l in out.splitlines() if "aiops-tuning" in l), "（沒有提示）")
            check(G, "aiops-tuning 讀不到（index 關閉）", "detect 照跑（不套調整）、印出提示",
                  f"跑完，ta {req(a, 'svc-ta')}；{note[:40]}", req(a, "svc-ta") == ["warning"] and "aiops-tuning" in note)
        except Exception as e:  # noqa: BLE001
            check(G, "aiops-tuning 讀不到（index 關閉）", "detect 照跑", f"失敗：{str(e)[:60]}", False)
    finally:
        es.req("POST", f"/{TUNING_INDEX}/_open?wait_for_active_shards=1")
    untune()
    put_tuning(es, "bedge-bad", {"kind": "exclude_window", "series": "tune\\..*", "match": {"service": "^svc-ta$"}, "start": at("10:00").isoformat()})
    put_tuning(es, "bedge-bad2", {"kind": "override", "series": "tune\\.http\\.(requests", "z": 50})
    tune("--z", "50")
    for cmd in ("train", "detect"):
        clean_results(es, "tune.", anomalies_only=True)
        try:
            out = train(config, T_END) if cmd == "train" else run(["detect", "--config", config, "--window", win, "--at", at_.tz_convert("UTC").isoformat()])
            note = next((l.strip() for l in out.splitlines() if "略過" in l), "（沒有提示）")
            ok = "略過" in note
            if cmd == "detect":
                a = anomalies(es, "tune.", T_END, DATA_END)
                ok = ok and req(a, "svc-ta") == [] and "anomaly" in lat(a, "svc-tb")
                note += f"；ta {req(a, 'svc-ta')}（好的 z=50 照樣生效）"
            check(G, f"aiops-tuning 有壞掉的調整（缺 end、正規表示式錯）：{cmd}", "略過壞的、其他照常", note[:80], ok)
        except Exception as e:  # noqa: BLE001
            check(G, f"aiops-tuning 有壞掉的調整（缺 end、正規表示式錯）：{cmd}", "略過壞的、其他照常", f"整個失敗：{type(e).__name__} {str(e)[:50]}", False)
    for i in ("bedge-bad", "bedge-bad2"):
        es.req("DELETE", f"/{TUNING_INDEX}/_doc/{i}?refresh=true", ok=(200, 404))
    untune()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-write", action="store_true", help="資料已經寫過，只跑 train／detect")
    ap.add_argument("--fresh", action="store_true", help="先刪掉上一次寫的資料")
    ap.add_argument("--only", default="fp,edge,reset,tune")
    args = ap.parse_args()
    es = ES()
    only = set(args.only.split(","))
    if not args.skip_write:
        t = pd.Timestamp.now()
        setup(es, args.fresh)
        ok, detail, names_hash = O.check_bulk_matches_otlp(es, resource("svc-chk", EDGE_DS), np.random.default_rng(3))
        check("寫入", "_bulk 寫的跟 OTLP 入口寫的一樣", "_source 逐欄一樣、p50/p95/p99 一樣", detail, ok)
        if not ok:
            return 1
        print("== 寫入合成資料", flush=True)
        n = write_metrics(es, SERVICES, INJECT, EDGE_DS, 7, names_hash) + write_metrics(es, TUNE_SERVICES, TUNE_INJECT, TUNE_DS, 11, names_hash)
        nl = write_logs(es, 13)
        for ds in (f"metrics-{EDGE_DS}.otel-it", f"metrics-{TUNE_DS}.otel-it", LOG_INDEX):
            es.req("POST", f"/{ds}/_refresh")
        print(f"  metrics {n} 個點、log {nl} 筆，{(pd.Timestamp.now() - t).total_seconds():.0f} 秒", flush=True)
    clean_tuning(es)
    config = make_config("edge", EDGE_DS)
    t = pd.Timestamp.now()
    if "fp" in only:
        phase_fp(es, config)
    if "edge" in only:
        phase_edge(es, config)
    if "reset" in only:
        phase_reset(es, config)
    if "tune" in only:
        phase_tune(es)
    print(f"\n== 結果（{(pd.Timestamp.now() - t).total_seconds():.0f} 秒）")
    for g, name, expect, actual, ok in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  [{g}] {name}｜預期 {expect}｜實際 {actual}")
    fail = sum(not r[4] for r in RESULTS)
    print(f"\n{len(RESULTS) - fail} PASS、{fail} FAIL")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

"""30 秒粒度的端到端測試：2 週＋1 天、每 30 秒一筆的模擬資料。

- busy-api（每 30 秒約 60 個請求）：自動選 30 秒；最後一天 10:00:00 起連續 90 秒延遲變 3 倍
  → 10:00:00 warning、10:00:30 anomaly（約 1 分鐘確認）；14:00:00 只尖 30 秒 → 只有 warning。
- quiet-api（每 30 秒約 1 個請求）：自動往粗調。
- tune：把 busy-api 的延遲固定成 1 分鐘，重新 train 後照用；刪掉後回到自動。

執行（在 aiops 底下）：ES_URL=... ES_AUTH=... python3 -m tests.fine_e2e
會建立／覆蓋 index aiops-fine，以及 aiops-baseline、aiops-anomalies 裡 series 名稱以 fine. 開頭的資料。
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from aiops.interface.cli.detect import main as cli
from aiops.core.entity.anomaly import ANOMALY_INDEX
from aiops.service.detect.es_facade import BASELINE_INDEX, ES

TZ = "Asia/Taipei"
INDEX = "aiops-fine"
CONFIG = "tests/fine.yaml"
TODAY = pd.Timestamp("2026-10-05", tz=TZ)            # 週一
START = TODAY - pd.Timedelta(weeks=2)
APIS = {"busy-api": 60.0, "quiet-api": 1.0}          # 白天每 30 秒平均幾個請求
SUSTAINED = ("busy-api", "10:00:00", 3)             # 連續 3 個 30 秒延遲變 3 倍
SPIKE = ("busy-api", "14:00:00")                    # 只尖 30 秒


def make() -> list[dict]:
    rng = np.random.default_rng(5)
    ts = pd.date_range(START, TODAY + pd.Timedelta(days=1), freq="30s", inclusive="left")
    t0 = TODAY + pd.Timedelta(SUSTAINED[1])
    bad = set(t0 + pd.Timedelta(seconds=30 * k) for k in range(SUSTAINED[2])) | {TODAY + pd.Timedelta(SPIKE[1])}
    rows = []
    for api, peak in APIS.items():
        lam = np.where((ts.hour >= 8) & (ts.hour < 20), peak, peak * 0.4)
        for t, k in zip(ts, rng.poisson(lam)):
            if k == 0:
                continue
            lat = rng.gamma(2.0, 50.0, k).mean() * (3.0 if api == "busy-api" and t in bad else 1.0)
            rows.append({"@timestamp": t.isoformat(), "api": api, "requests": int(k), "latency_ms": float(lat)})
    return rows


def clean(es: ES):
    for idx in (BASELINE_INDEX, ANOMALY_INDEX):
        es.req("POST", f"/{idx}/_delete_by_query?refresh=true&ignore_unavailable=true",
               {"query": {"prefix": {"series": "fine."}}}, ok=(200, 404))


def resolution_of(es: ES, series: str, api: str) -> str | None:
    es.req("POST", f"/{BASELINE_INDEX}/_refresh")
    h = es.req("POST", f"/{BASELINE_INDEX}/_search", {"size": 1, "query": {"bool": {"filter": [
        {"term": {"series": series}}, {"term": {"keys.api": api}}]}}})["hits"]["hits"]
    return h[0]["_source"].get("resolution") if h else None


def main() -> int:
    es = ES()
    es.req("DELETE", f"/{INDEX}", ok=(200, 404))
    es.req("PUT", f"/{INDEX}", {"mappings": {"properties": {
        "@timestamp": {"type": "date"}, "api": {"type": "keyword"}, "requests": {"type": "long"}, "latency_ms": {"type": "double"}}}})
    rows = make()
    for i in range(0, len(rows), 10000):
        lines = []
        for d in rows[i:i + 10000]:
            lines += [json.dumps({"index": {"_index": INDEX}}), json.dumps(d)]
        r = es.s.post(f"{es.url}/_bulk?refresh=wait_for", data="\n".join(lines) + "\n",
                      headers={"Content-Type": "application/x-ndjson"}, timeout=300)
        if r.json().get("errors"):
            raise RuntimeError(r.text[:500])
    print(f"寫入 {len(rows)} 筆（每 30 秒一筆，{START:%m-%d}～{TODAY + pd.Timedelta(days=1):%m-%d}）")
    clean(es)
    end_hist = TODAY.tz_convert("UTC").isoformat()
    t = pd.Timestamp.now()
    cli(["train", "--config", CONFIG, "--end", end_hist])
    train_s = (pd.Timestamp.now() - t).total_seconds()
    t = pd.Timestamp.now()
    cli(["detect", "--config", CONFIG, "--window", "1D", "--at", (TODAY + pd.Timedelta(days=1)).tz_convert("UTC").isoformat()])
    detect_s = (pd.Timestamp.now() - t).total_seconds()
    es.req("POST", f"/{ANOMALY_INDEX}/_refresh")
    hits = es.req("POST", f"/{ANOMALY_INDEX}/_search", {"size": 2000, "query": {"prefix": {"series": "fine."}}})["hits"]["hits"]
    got = {(h["_source"]["series"], h["_source"]["keys"]["api"],
            pd.Timestamp(h["_source"]["@timestamp"]).tz_convert(TZ).strftime("%H:%M:%S")): h["_source"]["status"] for h in hits}

    t0 = TODAY + pd.Timedelta(SUSTAINED[1])
    sus = [got.get(("fine.latency", "busy-api", (t0 + pd.Timedelta(seconds=30 * k)).strftime("%H:%M:%S")))
           for k in range(SUSTAINED[2])]
    spike = got.get(("fine.latency", "busy-api", SPIKE[1]))
    expected = {("fine.latency", "busy-api", (t0 + pd.Timedelta(seconds=30 * k)).strftime("%H:%M:%S")) for k in range(SUSTAINED[2])}
    expected.add(("fine.latency", "busy-api", SPIKE[1]))
    false = {k: v for k, v in got.items() if k not in expected}
    false_anom = {k: v for k, v in false.items() if v == "anomaly"}
    res_busy, res_quiet = resolution_of(es, "fine.latency", "busy-api"), resolution_of(es, "fine.requests", "quiet-api")

    print("\n== tune：把 busy-api 的延遲固定成 1 分鐘，重新 train")
    cli(["tune", "--config", CONFIG, "--series", "fine.latency", "--match", "api=^busy", "--resolution", "1m",
         "--reason", "測試", "--by", "fine_e2e"])
    cli(["train", "--config", CONFIG, "--end", end_hist])
    res_tuned = resolution_of(es, "fine.latency", "busy-api")
    cli(["tune", "--config", CONFIG, "--series", "fine.latency", "--match", "api=^busy", "--remove"])
    cli(["train", "--config", CONFIG, "--end", end_hist])
    res_back = resolution_of(es, "fine.latency", "busy-api")

    points = 2880 * len(APIS) * 2
    checks = [
        (f"忙的 API 自動用 30 秒（{res_busy}）", res_busy == "30s"),
        (f"少人用的 API 自動往粗調（請求數 {res_quiet}）", res_quiet not in (None, "30s")),
        (f"連續 90 秒變慢 → {sus}（應該是 warning、anomaly、anomaly：約 1 分鐘確認）", sus == ["warning", "anomaly", "anomaly"]),
        (f"只尖 30 秒 → {spike}（應該只有 warning）", spike == "warning"),
        (f"正常的點：anomaly 誤報 {len(false_anom)} 個、warning {len(false) - len(false_anom)} 個／{points} 個點", len(false_anom) == 0),
        (f"tune 固定 1 分鐘 → 重新 train 後是 {res_tuned}；刪掉後回到 {res_back}", res_tuned == "1m" and res_back == "30s"),
    ]
    print(f"\n== 結果（train {train_s:.0f} 秒、detect 一天 {detect_s:.0f} 秒）")
    fail = 0
    for name, ok in checks:
        fail += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    for k, v in list(false.items())[:5]:
        print(f"         其他：{k} {v}")
    print("\n全部通過" if not fail else "\n有項目失敗")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

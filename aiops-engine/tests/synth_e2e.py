"""模擬資料的端到端測試：寫 4 週＋1 天的模擬歷史進 ES，最後一天插入已知的異常，
跑 train（只用前 4 週）與 detect（最後一天），檢查抓到了哪些、正常的點誤報多少。

執行（在 aiops 底下）：ES_URL=... ES_AUTH=... python3 -m tests.synth_e2e
會建立／覆蓋 index aiops-synth，以及 aiops-baseline、aiops-anomalies 裡 series 名稱以 synth. 開頭的資料。
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from interface.cli.detect import main as cli
from detect.store.anomaly import ANOMALY_INDEX
from shared.infra.es import ES
from detect.store.baseline import BASELINE_INDEX

TZ = "Asia/Taipei"
INDEX = "aiops-synth"
CONFIG = "tests/synth.yaml"
# 最後一天（「今天」）是週一，台北時間
TODAY = pd.Timestamp("2026-10-05", tz=TZ)
HISTORY_START = TODAY - pd.Timedelta(weeks=4)
SERVICES = {"mes-api": 100.0, "wip-worker": 40.0, "eap-gw": 300.0}   # 平日白天每 5 分鐘的請求數

# 插入的異常（台北時間）：(服務, 時間, 欄位, 怎麼改, 說明)
INJECT = [
    ("mes-api", "10:00", "requests", lambda v: v * 3, "流量暴增 3 倍"),
    ("wip-worker", "14:30", "latency_ms", lambda v: v * 4, "延遲變 4 倍"),
    ("eap-gw", "02:00", "requests", lambda v: 300.0, "半夜出現白天的流量"),
    ("mes-api", "16:00", "errors", lambda v: v + 40, "錯誤數暴增"),
]


# 連續的異常（台北時間）：mes-api 18:00 起連續 3 個 5 分鐘錯誤數暴增 → 18:00 warning、18:05、18:10 anomaly
SUSTAINED = ("mes-api", "18:00", 3, "errors", lambda v: v + 40)


def make() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    ts = pd.date_range(HISTORY_START, TODAY + pd.Timedelta(days=1), freq="5min", inclusive="left")
    rows = []
    for svc, peak in SERVICES.items():
        day = (ts.hour >= 8) & (ts.hour < 20)
        weekend = ts.dayofweek >= 5
        req = np.where(day, peak, peak * 0.2) * np.where(weekend, 0.5, 1.0)
        req = np.maximum(0, req + rng.normal(0, req * 0.08))
        lat = np.where(day, 120.0, 80.0) + rng.normal(0, 8, len(ts))         # 白天比較忙、稍慢
        err = rng.poisson(np.where(day, 2.0, 0.5))
        rows.append(pd.DataFrame({"ts": ts, "service": svc, "requests": req.round(), "latency_ms": lat, "errors": err.astype(float)}))
    df = pd.concat(rows, ignore_index=True)
    for svc, hhmm, field, fn, _ in INJECT:
        at = TODAY + pd.Timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[3:]))
        i = df.index[(df.service == svc) & (df.ts == at)]
        df.loc[i, field] = df.loc[i, field].map(fn)
    svc, hhmm, n, field, fn = SUSTAINED
    for k in range(n):
        at = TODAY + pd.Timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[3:]) + 5 * k)
        i = df.index[(df.service == svc) & (df.ts == at)]
        df.loc[i, field] = df.loc[i, field].map(fn)
    return df


def main() -> int:
    es = ES()
    es.req("DELETE", f"/{INDEX}", ok=(200, 404))
    es.req("PUT", f"/{INDEX}", {"mappings": {"properties": {
        "@timestamp": {"type": "date"}, "service": {"type": "keyword"},
        "requests": {"type": "double"}, "latency_ms": {"type": "double"}, "errors": {"type": "double"}}}})
    df = make()
    docs = [(None, {"@timestamp": r.ts.isoformat(), "service": r.service, "requests": r.requests,
                    "latency_ms": r.latency_ms, "errors": r.errors}) for r in df.itertuples()]
    print(f"寫入模擬資料 {len(docs)} 筆（{df.service.nunique()} 個服務，{HISTORY_START:%m-%d}～{TODAY + pd.Timedelta(days=1):%m-%d}）")
    _bulk_no_id(es, docs)
    for idx in (BASELINE_INDEX, ANOMALY_INDEX):
        es.req("POST", f"/{idx}/_delete_by_query?refresh=true&ignore_unavailable=true",
               {"query": {"prefix": {"series": "synth."}}}, ok=(200, 404))

    end_of_history = TODAY.tz_convert("UTC").isoformat()
    end_of_today = (TODAY + pd.Timedelta(days=1)).tz_convert("UTC").isoformat()
    print("\n== train（只用前 4 週）")
    cli(["train", "--config", CONFIG, "--end", end_of_history])
    print("\n== detect（最後一天）")
    cli(["detect", "--config", CONFIG, "--window", "1D", "--at", end_of_today])

    es.req("POST", f"/{ANOMALY_INDEX}/_refresh")
    hits = es.req("POST", f"/{ANOMALY_INDEX}/_search", {"size": 1000, "query": {"prefix": {"series": "synth."}}})["hits"]["hits"]
    status = {(h["_source"]["series"], h["_source"]["keys"]["service"],
               pd.Timestamp(h["_source"]["@timestamp"]).tz_convert(TZ).strftime("%H:%M")): h["_source"].get("status")
              for h in hits}
    found = set(status)
    series_of = {"requests": "synth.requests", "latency_ms": "synth.latency", "errors": "synth.errors"}
    svc_s, hhmm_s, n_s, field_s, _ = SUSTAINED
    sustained = [(series_of[field_s], svc_s, (pd.Timedelta(hhmm_s + ":00") + pd.Timedelta(minutes=5 * k)).components)
                 for k in range(n_s)]
    sustained = [(s_, v_, f"{c.hours:02d}:{c.minutes:02d}") for s_, v_, c in sustained]
    expected = {(series_of[f], svc, hhmm) for svc, hhmm, f, _, _ in INJECT} | set(sustained)

    print("\n== 結果")
    fail = 0
    for svc, hhmm, f, _, desc in INJECT:
        st = status.get((series_of[f], svc, hhmm))
        ok = st == "warning"
        fail += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {svc} {hhmm} {desc}：只跳一個點 → {st}（應該是 warning）")
    got = [status.get(k) for k in sustained]
    ok = got == ["warning"] + ["anomaly"] * (n_s - 1)
    fail += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {svc_s} {hhmm_s} 起連續 {n_s} 個 5 分鐘錯誤數暴增 → {got}（應該是 warning、anomaly、anomaly）")

    false_pos = found - expected
    points = len(SERVICES) * 288 * len(series_of)
    rate = len(false_pos) / points
    ok = rate <= 0.005
    fail += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  正常的點誤報 {len(false_pos)} 個／{points} 個（{rate:.2%}，上限 0.5%）")
    for fp in sorted(false_pos)[:10]:
        print(f"         誤報：{fp}")
    print("\n全部通過" if not fail else "\n有項目失敗")
    return 1 if fail else 0


def _bulk_no_id(es: ES, docs):
    import json
    for i in range(0, len(docs), 5000):
        lines = []
        for _, d in docs[i:i + 5000]:
            lines.append(json.dumps({"index": {"_index": INDEX}}))
            lines.append(json.dumps(d))
        r = es.s.post(f"{es.url}/_bulk?refresh=wait_for", data="\n".join(lines) + "\n",
                      headers={"Content-Type": "application/x-ndjson"}, timeout=300)
        if r.json().get("errors"):
            raise RuntimeError(r.text[:500])


if __name__ == "__main__":
    sys.exit(main())

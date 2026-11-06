"""自動選粒度的端到端測試：一支忙的 API、一支很少人用的 API，最後一天各插入一段「延遲變 3 倍、持續 1 小時」；
3 台設備的告警（平常每小時約 0.3 次），最後一天 EQP-02 在 14 點那一小時多了 8 次。
另有一支最後一天才上線的 new-query（沒有歷史），同一段時間也變慢：要借同組（*-query）的 baseline 抓到。
分別用「每條序列自己選粒度」（adaptive.yaml）和「固定 5 分鐘」（同一份設定拿掉 adaptive）訓練、偵測，比較結果。

執行（在 aiops 底下）：ES_URL=... ES_AUTH=... python3 -m tests.adaptive_e2e
會建立／覆蓋 index aiops-adaptive、aiops-adaptive-alarm，以及 aiops-baseline、aiops-anomalies 裡 series 名稱以 adaptive. 開頭的資料。
"""
from __future__ import annotations

import json
import sys
import tempfile

import numpy as np
import pandas as pd
import yaml

from aiops.interface.cli.detect import main as cli
from aiops.core.entity.anomaly import ANOMALY_INDEX
from aiops.service.detect.es_facade import BASELINE_INDEX, ES

TZ = "Asia/Taipei"
INDEX = "aiops-adaptive"
CONFIG = "tests/adaptive.yaml"
TODAY = pd.Timestamp("2026-10-05", tz=TZ)          # 週一
HISTORY_START = TODAY - pd.Timedelta(weeks=4)
APIS = {"busy-query": 100.0, "rare-query": 2.0,     # 平日白天每 5 分鐘平均幾個請求
        "lot-query": 60.0, "wip-query": 80.0, "new-query": 50.0}
NEW_API = "new-query"                               # 最後一天才上線
INCIDENT = ("10:00", "11:00")                        # 最後一天這段時間兩支 API 的延遲都變 3 倍
ALARM_INDEX = "aiops-adaptive-alarm"
EQUIPMENT = ["EQP-01", "EQP-02", "EQP-03"]
ALARM_BURST = ("EQP-02", "14:00", 8)                 # 最後一天 EQP-02 在 14 點那一小時多了 8 次告警


def make_alarms() -> list[dict]:
    rng = np.random.default_rng(11)
    hours = pd.date_range(HISTORY_START, TODAY + pd.Timedelta(days=1), freq="1h", inclusive="left")
    rows = []
    for eq in EQUIPMENT:
        for h, k in zip(hours, rng.poisson(0.3, len(hours))):
            rows += [{"@timestamp": (h + pd.Timedelta(seconds=int(rng.integers(3600)))).isoformat(), "equipment": eq}
                     for _ in range(k)]
    eq, hhmm, k = ALARM_BURST
    h = TODAY + pd.Timedelta(hhmm + ":00")
    rows += [{"@timestamp": (h + pd.Timedelta(seconds=int(rng.integers(3600)))).isoformat(), "equipment": eq} for _ in range(k)]
    return rows


def make() -> pd.DataFrame:
    """每 5 分鐘一筆：請求數（卜瓦松）、這幾個請求的平均延遲（每個請求約 100ms，個別差很多）。沒有請求的 5 分鐘沒有資料。"""
    rng = np.random.default_rng(7)
    ts = pd.date_range(HISTORY_START, TODAY + pd.Timedelta(days=1), freq="5min", inclusive="left")
    inc = (ts >= TODAY + pd.Timedelta(INCIDENT[0] + ":00")) & (ts < TODAY + pd.Timedelta(INCIDENT[1] + ":00"))
    rows = []
    for api, peak in APIS.items():
        day = (ts.hour >= 8) & (ts.hour < 20)
        lam = np.where(day, peak, peak * 0.3) * np.where(ts.dayofweek >= 5, 0.5, 1.0)
        n = rng.poisson(lam)
        for t, k, bad in zip(ts, n, inc):
            if k == 0 or (api == NEW_API and t < TODAY):
                continue
            lat = rng.gamma(2.0, 50.0, k) * (3.0 if bad else 1.0)   # 平均 100ms、個別請求從 10ms 到 300ms 都有
            rows.append({"@timestamp": t.isoformat(), "api": api, "requests": int(k), "latency_ms": float(lat.mean())})
    return pd.DataFrame(rows)


def run(es: ES, config: str, label: str) -> set:
    for idx in (BASELINE_INDEX, ANOMALY_INDEX):
        es.req("POST", f"/{idx}/_delete_by_query?refresh=true&ignore_unavailable=true",
               {"query": {"prefix": {"series": "adaptive."}}}, ok=(200, 404))
    print(f"\n==== {label}")
    cli(["train", "--config", config, "--end", TODAY.tz_convert("UTC").isoformat()])
    cli(["detect", "--config", config, "--window", "1D", "--at", (TODAY + pd.Timedelta(days=1)).tz_convert("UTC").isoformat()])
    es.req("POST", f"/{ANOMALY_INDEX}/_refresh")
    hits = es.req("POST", f"/{ANOMALY_INDEX}/_search", {"size": 2000, "query": {"prefix": {"series": "adaptive."}}})["hits"]["hits"]
    return {(h["_source"]["series"], h["_source"]["keys"].get("api") or h["_source"]["keys"].get("equipment"),
             pd.Timestamp(h["_source"]["@timestamp"]).tz_convert(TZ).strftime("%H:%M"),
             h["_source"].get("resolution"), h["_source"].get("confidence")) for h in hits}


def overlaps(hhmm: str, resolution: str | None, period: tuple[str, str]) -> bool:
    """異常的時間點（時間窗的開頭）跟事故那段有重疊就算抓到。"""
    start = pd.Timedelta(hhmm + ":00")
    end = start + pd.Timedelta((resolution or "5m").replace("m", "min"))
    return start < pd.Timedelta(period[1] + ":00") and end > pd.Timedelta(period[0] + ":00")


def expected(series: str, who: str, hhmm: str, res: str | None) -> bool:
    if series == "adaptive.latency":
        return overlaps(hhmm, res, INCIDENT)
    if series == "adaptive.alarms":
        eq, start, _ = ALARM_BURST
        return who == eq and overlaps(hhmm, res, (start, f"{int(start[:2]) + 1:02d}:00"))
    return False


def report(found: set) -> tuple[dict, dict]:
    hit, false = {}, {}
    for series, api, hhmm, res, conf in found:
        if expected(series, api, hhmm, res):
            hit.setdefault(api, []).append((hhmm, res, conf))
        else:
            false.setdefault(api, []).append((series, hhmm, res))
    for api in [*APIS, *EQUIPMENT]:
        h, f = sorted(hit.get(api, [])), sorted(false.get(api, []))
        print(f"  {api}：抓到事故 {len(h)} 個點 {h[:3]}{'…' if len(h) > 3 else ''}；誤報 {len(f)} 個 {f[:3]}{'…' if len(f) > 3 else ''}")
    return hit, false


def main() -> int:
    es = ES()
    es.req("DELETE", f"/{INDEX}", ok=(200, 404))
    es.req("PUT", f"/{INDEX}", {"mappings": {"properties": {
        "@timestamp": {"type": "date"}, "api": {"type": "keyword"}, "requests": {"type": "long"}, "latency_ms": {"type": "double"}}}})
    df = make()
    for i in range(0, len(df), 5000):
        lines = []
        for d in df.iloc[i:i + 5000].to_dict("records"):
            lines += [json.dumps({"index": {"_index": INDEX}}), json.dumps(d)]
        r = es.s.post(f"{es.url}/_bulk?refresh=wait_for", data="\n".join(lines) + "\n",
                      headers={"Content-Type": "application/x-ndjson"}, timeout=300)
        if r.json().get("errors"):
            raise RuntimeError(r.text[:500])
    es.req("DELETE", f"/{ALARM_INDEX}", ok=(200, 404))
    es.req("PUT", f"/{ALARM_INDEX}", {"mappings": {"properties": {"@timestamp": {"type": "date"}, "equipment": {"type": "keyword"}}}})
    alarms = make_alarms()
    lines = []
    for d in alarms:
        lines += [json.dumps({"index": {"_index": ALARM_INDEX}}), json.dumps(d)]
    es.s.post(f"{es.url}/_bulk?refresh=wait_for", data="\n".join(lines) + "\n",
              headers={"Content-Type": "application/x-ndjson"}, timeout=300).raise_for_status()
    print(f"設備告警 {len(alarms)} 筆（{len(EQUIPMENT)} 台，平常每小時約 0.3 次；最後一天 {ALARM_BURST[0]} {ALARM_BURST[1]} 多 {ALARM_BURST[2]} 次）")
    print(f"寫入 {len(df)} 筆（{HISTORY_START:%m-%d}～{TODAY + pd.Timedelta(days=1):%m-%d}）；"
          f"最後一天 {INCIDENT[0]}～{INCIDENT[1]} 兩支 API 的延遲都變 3 倍")

    with open(CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    fixed_cfg = {k: v for k, v in cfg.items() if k != "adaptive"}
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(fixed_cfg, f, allow_unicode=True)
        fixed_path = f.name

    print("\n== 結果：固定 5 分鐘")
    fixed_hit, fixed_false = report(run(es, fixed_path, "固定 5 分鐘"))
    found = run(es, CONFIG, "自己選粒度")
    print("\n== 結果：自己選粒度")
    hit, false = report(found)

    checks = [
        ("忙的 API 維持 5 分鐘、抓到事故", any(r == "5m" for _, r, _ in hit.get("busy-query", []))),
        ("少人用的 API 改成 1 小時、抓到事故", any(r == "1h" for _, r, _ in hit.get("rare-query", []))),
        ("少人用的 API 標可信度（不是 high）", all(c != "high" for _, r, c in hit.get("rare-query", []))),
        ("新上線的 API：借同組 baseline 抓到、可信度 low", any(c == "low" for _, r, c in hit.get(NEW_API, []))
         and all(c == "low" for _, r, c in hit.get(NEW_API, []))),
        ("新上線的 API：沒有誤報", not false.get(NEW_API)),
        ("設備告警爆量：卜瓦松（1 小時）抓到", any(r == "1h" for _, r, _ in hit.get(ALARM_BURST[0], []))),
        ("自己選粒度：誤報 ≤ 3 個", sum(len(v) for v in false.values()) <= 3),
        ("自己選粒度的誤報比固定 5 分鐘少、或一樣", sum(len(v) for v in false.values()) <= sum(len(v) for v in fixed_false.values())),
    ]
    print(f"\n  參考：固定 5 分鐘時 {ALARM_BURST[0]} 的告警爆量"
          f"{'碰巧抓到（剛好有幾次擠在同一個 5 分鐘）' if fixed_hit.get(ALARM_BURST[0]) else '抓不到（分散在一小時裡，每 5 分鐘只有 0～2 次）'}")
    print()
    fail = 0
    for name, ok in checks:
        fail += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("\n全部通過" if not fail else "\n有項目失敗")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

"""合成 Elastic APM 的資料（整合儀表板、APM 來源的驗證用）：三種索引，欄位照 configs/apm/README.md 的對照表。

  （ES 8.x 內建 APM 的 index template，三個名字都是 data stream，mapping 跟真的 APM 一樣，不用自己建）
  metrics-apm.transaction.1m-default   每分鐘一筆（每個服務 × Pod × API × 結果）：transaction.duration.histogram（ES histogram，微秒）
  traces-apm-default                    transaction、span 各一筆（processor.event）：trace.id、parent.id、*.duration.us、span.destination.service.resource
  logs-apm.error-default                agent 回報的例外：processor.event: error、error.exception.message、trace.id

情境：mes-api 的 GET /lots/{id} 呼叫 wip-worker 的 GET /wip，wip-worker 查 Oracle（SELECT LOT_HISTORY）。
前 N 分鐘正常；最後 INCIDENT 分鐘 wip-worker 等 Oracle 連線池逾時 → 整條鏈變慢、約一成請求失敗、error log 一直噴。

用法（在 aiops-engine/ 底下）：
  ES_URL=http://127.0.0.1:9200 ES_AUTH=x:x python3 -X utf8 -m tests.apm_synth [--minutes 90] [--incident 25] [--clean]
不寫任何 aiops-* 的文件；之後用 engine 自己跑 rules、trace-merge、alert 產生事件（tests/apm_dashboard_e2e.py 就是這樣驗的）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

METRICS, TRACES, ERRORS = "metrics-apm.transaction.1m-default", "traces-apm-default", "logs-apm.error-default"
DEPT, GROUP = "D3120", "mes"          # agent 設的 labels.dept、labels.group（告警依它分送）
ORACLE_MSG = "Connection pool timeout waiting for connection to 10.0.1.5:5432 after 1500ms"

# (服務, API, 每分鐘請求數, 平常的中位數 ms, Pod 數)
APIS = [("mes-api", "GET /lots/{id}", 60, 70, 2), ("mes-api", "POST /lots", 20, 40, 2), ("wip-worker", "GET /wip", 60, 55, 2)]
INCIDENT_MEDIAN_MS = {("mes-api", "GET /lots/{id}"): 1700, ("wip-worker", "GET /wip"): 1600}
INCIDENT_ERROR_RATE = {("mes-api", "GET /lots/{id}"): 0.12, ("wip-worker", "GET /wip"): 0.12}

def put(es: str, path: str, body=None, method="PUT"):
    r = requests.request(method, es + path, json=body, timeout=60)
    if r.status_code >= 300:
        raise SystemExit(f"{method} {path} → {r.status_code} {r.text[:300]}")
    return r.json()


def histogram(rng: random.Random, n: int, median_ms: float, sigma: float = 0.4) -> dict:
    """n 個請求的耗時（對數常態）→ ES histogram：values 是桶的中心（微秒）、counts 是個數。桶約 6% 寬。"""
    buckets: dict[int, int] = {}
    for _ in range(n):
        us = rng.lognormvariate(math.log(median_ms * 1000), sigma)
        buckets[round(math.log(us, 1.06))] = buckets.get(round(math.log(us, 1.06)), 0) + 1
    keys = sorted(buckets)
    return {"values": [round(1.06 ** k, 1) for k in keys], "counts": [buckets[k] for k in keys]}


def metric_docs(rng: random.Random, minute: datetime, incident: bool) -> list[dict]:
    out = []
    for svc, name, rate, median, pods in APIS:
        med = INCIDENT_MEDIAN_MS.get((svc, name), median) if incident else median
        err = INCIDENT_ERROR_RATE.get((svc, name), 0.005) if incident else 0.005
        for p in range(pods):
            n = max(1, int(rate / pods * rng.uniform(0.9, 1.1)))
            failed = sum(rng.random() < err for _ in range(n))
            for outcome, k in (("success", n - failed), ("failure", failed)):
                if k <= 0:
                    continue
                out.append({"@timestamp": minute.isoformat(), "metricset.name": "transaction", "transaction.type": "request",
                            "service.name": svc, "service.node.name": f"{svc}-{p}", "transaction.name": name, "event.outcome": outcome,
                            "transaction.duration.histogram": histogram(rng, k, med * (1.2 if outcome == "failure" else 1.0)),
                            "labels.dept": DEPT, "labels.group": GROUP, "kubernetes.namespace": "mes"})
    return out


def trace_docs(rng: random.Random, t: datetime, incident: bool, i: int) -> tuple[list[dict], list[dict]]:
    """一個 GET /lots/{id} 請求：mes-api 的 transaction → 呼叫 wip-worker 的 span → wip-worker 的 transaction → 查 Oracle 的 span。"""
    tid = f"{rng.getrandbits(128):032x}"
    ids = [f"{rng.getrandbits(64):016x}" for _ in range(4)]
    tx1, s_call, tx2, s_db = ids
    fail = incident and rng.random() < INCIDENT_ERROR_RATE[("mes-api", "GET /lots/{id}")]
    db_ms = rng.lognormvariate(math.log(1500 if incident else 40), 0.25)
    wip_ms = db_ms + rng.uniform(2, 6)
    own_lot_ms = rng.uniform(3, 8)
    call_ms = wip_ms + rng.uniform(1, 4)
    total_ms = call_ms + own_lot_ms + rng.uniform(2, 6)
    outcome = "failure" if fail else "success"
    pod1, pod2 = f"mes-api-{rng.randrange(2)}", f"wip-worker-{rng.randrange(2)}"
    ing = (t + timedelta(seconds=1)).isoformat()
    base = {"trace.id": tid, "event.ingested": ing, "labels.dept": DEPT, "labels.group": GROUP}

    def txn(service, pod, tx_id, parent, name, ms, start):
        d = {**base, "@timestamp": start.isoformat(), "processor.event": "transaction", "service.name": service, "service.node.name": pod,
             "kubernetes.pod.name": pod, "transaction.id": tx_id, "transaction.name": name, "transaction.type": "request",
             "transaction.duration.us": int(ms * 1000), "event.outcome": outcome}
        if parent:
            d["parent.id"] = parent
        return d

    def span(service, pod, tx_id, sid, parent, name, typ, sub, dest, ms, start, stmt=None):
        d = {**base, "@timestamp": start.isoformat(), "processor.event": "span", "service.name": service, "service.node.name": pod,
             "kubernetes.pod.name": pod, "transaction.id": tx_id, "span.id": sid, "parent.id": parent, "span.name": name,
             "span.type": typ, "span.subtype": sub, "span.duration.us": int(ms * 1000), "span.destination.service.resource": dest,
             "event.outcome": outcome}
        if stmt:
            d["span.db.statement"] = stmt
        return d

    docs = [
        txn("mes-api", pod1, tx1, None, "GET /lots/{id}", total_ms, t),
        span("mes-api", pod1, tx1, s_call, tx1, "GET wip-worker/wip", "external", "http", "wip-worker", call_ms, t + timedelta(milliseconds=3)),
        txn("wip-worker", pod2, tx2, s_call, "GET /wip", wip_ms, t + timedelta(milliseconds=5)),
        span("wip-worker", pod2, tx2, s_db, tx2, "SELECT LOT_HISTORY", "db", "oracle", "oracle", db_ms, t + timedelta(milliseconds=7),
             "SELECT * FROM LOT_HISTORY WHERE LOT_ID = ? ORDER BY STEP_SEQ"),
    ]
    errs = []
    if fail or (incident and rng.random() < 0.5):
        errs.append({"@timestamp": (t + timedelta(milliseconds=int(db_ms))).isoformat(), "processor.event": "error", "service.name": "wip-worker",
                     "trace.id": tid, "transaction.id": tx2, "error.exception.message": ORACLE_MSG + f", lot L{107900 + i}",
                     "error.exception.type": "TimeoutError", "error.grouping_key": "oracle-pool-timeout", "message": ORACLE_MSG})
    return docs, errs


def nest(flat: dict) -> dict:
    """{"a.b.c": 1} → {"a": {"b": {"c": 1}}}。apm-server 寫的是巢狀物件；用點號的鍵會讓 ES 內建的 APM pipeline 以為 processor.event 沒填、補上預設值。"""
    out: dict = {}
    for k, v in flat.items():
        cur = out
        *head, last = k.split(".")
        for h in head:
            cur = cur.setdefault(h, {})
        cur[last] = v
    return out


def merged_template() -> dict:
    """traces-merged-* 的 index template：elasticsearch/kibana-dev-tools.txt 第 2 段（真環境由 ES 管理員貼；這裡照樣放，才有 nested）。"""
    txt = (Path(__file__).resolve().parents[2] / "elasticsearch" / "kibana-dev-tools.txt").read_text(encoding="utf-8")
    m = re.search(r"PUT _index_template/traces-merged\n(\{.*?\n\})\n", txt, re.S)
    body = json.loads(m.group(1))
    body.get("template", {}).get("settings", {}).pop("index.lifecycle.name", None)   # 測試 ES 沒有那條 ILM
    return body


def load(es: str, now: datetime | None = None, minutes: int = 90, incident: int = 25, seed: int = 7, clean: bool = False) -> dict:
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    rng = random.Random(seed)
    for ix in (METRICS, TRACES, ERRORS):     # ES 8.x 內建 APM 的 index template（apm-data），這三個名字會自動建成 data stream，欄位型別跟真的 APM 一樣
        if clean:
            requests.delete(f"{es}/_data_stream/{ix}", timeout=30)
    put(es, "/_index_template/traces-merged", merged_template())
    lines = {METRICS: [], TRACES: [], ERRORS: []}
    start = now - timedelta(minutes=minutes)
    n_req = 0
    for m in range(minutes):
        minute = start + timedelta(minutes=m)
        inc = m >= minutes - incident
        lines[METRICS] += metric_docs(rng, minute, inc)
        for k in range(6):
            t = minute + timedelta(seconds=rng.uniform(1, 55))
            d, e = trace_docs(rng, t, inc, n_req)
            lines[TRACES] += d
            lines[ERRORS] += e
            n_req += 1
    for ix, docs in lines.items():
        body = "".join(json.dumps({"create": {"_index": ix}}) + "\n" + json.dumps(nest(d), ensure_ascii=False) + "\n" for d in docs)
        r = requests.post(f"{es}/_bulk?refresh=true", data=body.encode("utf-8"), headers={"Content-Type": "application/x-ndjson"}, timeout=120)
        if r.json().get("errors"):
            bad = [i for i in r.json()["items"] if next(iter(i.values())).get("error")][:2]
            raise SystemExit(f"寫 {ix} 有錯：{bad}")
    return {"metrics": len(lines[METRICS]), "spans": len(lines[TRACES]), "errors": len(lines[ERRORS]), "now": now.isoformat(),
            "incident_from": (now - timedelta(minutes=incident)).isoformat()}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=90)
    ap.add_argument("--incident", type=int, default=25)
    ap.add_argument("--clean", action="store_true", help="先刪掉三個 data stream 再寫")
    a = ap.parse_args()
    print(json.dumps(load(os.environ["ES_URL"], minutes=a.minutes, incident=a.incident, clean=a.clean), ensure_ascii=False))

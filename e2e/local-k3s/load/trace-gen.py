#!/usr/bin/env python3
"""故障測試用的 trace 產生器：每秒 RATE 個 trace（每個 2 段），每段帶 lt.run、lt.seq，事後在 ES 數哪些序號不見。

送法照 OpenTelemetry Java SDK 的預設，量到的「應用端丟掉」才跟真的一樣：
  - BatchSpanProcessor：每 BATCH_DELAY 秒（預設 1）送一次、一次最多 512 段；佇列最多 2,048 段，滿了新的段直接丟
  - OTLP exporter 的重試：最多 5 次、退避 1 秒起 ×1.5、最多 5 秒；連不上、逾時、429、502、503、504 才重試；
    每次逾時 10 秒。5 次都失敗那一批就丟
序號 n 的產生時間 = start + (n-1)/RATE。結束（或收到 SIGTERM）時在 stderr 印一行 JSON：
  {"run", "trace_sent": 產生幾個, "trace_app_dropped": 應用端丟掉幾個, "start"}
環境變數：OTLP、RATE、DURATION、RUN、BATCH_DELAY。只用 Python 內建套件。
"""
import json
import os
import random
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

EP = os.environ.get("OTLP", "http://otel-node-collector.otel-collector.svc:4318/v1/traces")
RATE = int(os.environ.get("RATE", "20"))
DURATION = float(os.environ.get("DURATION", "300"))
RUN = os.environ.get("RUN", "run")
BATCH_DELAY = float(os.environ.get("BATCH_DELAY", "1"))
MAX_QUEUE, MAX_BATCH = 2048, 512
RETRYABLE = {429, 502, 503, 504}

queue, lock = [], threading.Lock()
dropped_seqs = set()
stopped = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stopped.set())


def attr(k, v):
    return {"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}


def spans(seq, t):
    tid, root = "%032x" % random.getrandbits(128), "%016x" % random.getrandbits(64)
    common = [attr("lt.run", RUN), attr("lt.seq", seq)]
    ns = lambda x: str(int(x * 1e9))
    return [
        {"traceId": tid, "spanId": root, "name": "GET /dur", "kind": 2,
         "startTimeUnixNano": ns(t), "endTimeUnixNano": ns(t + 0.012), "attributes": common, "status": {}},
        {"traceId": tid, "spanId": "%016x" % random.getrandbits(64), "parentSpanId": root, "name": "db", "kind": 3,
         "startTimeUnixNano": ns(t + 0.002), "endTimeUnixNano": ns(t + 0.010), "attributes": common, "status": {}},
    ]


def post(batch):
    body = json.dumps({"resourceSpans": [{"resource": {"attributes": [attr("service.name", "dur-trace")]},
                                          "scopeSpans": [{"scope": {"name": "dur"}, "spans": batch}]}]}).encode()
    backoff = 1.0
    for attempt in range(5):
        try:
            req = urllib.request.Request(EP, data=body, method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10):
                return True
        except urllib.error.HTTPError as e:
            if e.code not in RETRYABLE:
                return False
        except OSError:
            pass
        if attempt < 4:
            time.sleep(random.uniform(0, backoff))
            backoff = min(backoff * 1.5, 5.0)
    return False


def exporter():
    while True:
        last = stopped.is_set() or done.is_set()
        time.sleep(0 if last else BATCH_DELAY)
        while True:
            with lock:
                batch = queue[:MAX_BATCH]
                del queue[:MAX_BATCH]
            if not batch:
                break
            if not post(batch):
                dropped_seqs.update(int(s["attributes"][1]["value"]["intValue"]) for s in batch)
        if last:
            return


done = threading.Event()
th = threading.Thread(target=exporter, daemon=True)
th.start()
start = time.time()
seq = 0
while not stopped.is_set():
    now = time.time()
    if now - start >= DURATION:
        break
    while seq < int((now - start) * RATE):
        seq += 1
        sp = spans(seq, start + (seq - 1) / RATE)
        with lock:
            if len(queue) + len(sp) <= MAX_QUEUE:
                queue.extend(sp)
            else:
                dropped_seqs.add(seq)
    time.sleep(0.01)
done.set()
th.join(timeout=60)
ranges = []
for s in sorted(dropped_seqs):
    if ranges and s == ranges[-1][1] + 1:
        ranges[-1][1] = s
    else:
        ranges.append([s, s])
sys.stderr.write(json.dumps({"run": RUN, "trace_sent": seq, "trace_app_dropped": len(dropped_seqs),
                             "trace_app_dropped_seqs": ranges[:20], "start": round(start, 3)}) + "\n")

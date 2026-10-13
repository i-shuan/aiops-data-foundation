#!/usr/bin/env python3
"""尾端取樣壓測：模擬兩個服務的 trace，用 OTLP/HTTP（JSON）送到 node Collector，只用 Python 內建的套件。

每個請求 4 段：lt-gateway 收到請求（SERVER）→ 呼叫 lt-order（CLIENT）→ lt-order 收到（SERVER）→ 查 DB（CLIENT）。
每個服務像真的 SDK 一樣「批次送」：一段結束後先放著，每 BATCH_DELAY 秒或滿 512 段才送一次（OpenTelemetry SDK 的預設），
兩個服務各自的時間點。所以同一個 trace 的 4 段會在不同時間、分好幾次到 Collector，跟真的一樣。

請求的種類（MIX，每一段都帶 lt.category、lt.run，事後用來對答案）：
  normal     89%  DB 5～40ms，全部 200
  error       4%  DB 那段 status ERROR，往上兩段 500、ERROR
  slow        3%  DB 1.5～3 秒
  very_slow   1%  DB 12 秒（每一段都 12 秒後才結束，會一起到）
  slow_after  1%  下游、DB 都很快，lt-gateway 自己之後又做了 12 秒：底下三段先到，最上面那段 12 秒後才到，
                  比 decision_wait 晚（決定的時候還看不到它，也就看不出整個請求很慢）
  swallowed   2%  DB 那段帶例外事件，但 status 沒設、全部 200（程式 catch 掉了）

環境變數：OTLP（送到哪）、RATE（每秒幾個請求）、DURATION（送幾秒）、BATCH_DELAY、RUN（這一輪的名字）。
結束時印一行 SENT {...}：每種送了幾個 trace。
"""
import heapq
import json
import os
import random
import sys
import time
import urllib.request

ENDPOINT = os.environ.get("OTLP", "http://otel-node-collector.otel-collector.svc:4318/v1/traces")
RATE = float(os.environ.get("RATE", "200"))
DURATION = float(os.environ.get("DURATION", "300"))
BATCH_DELAY = float(os.environ.get("BATCH_DELAY", "5"))
MAX_BATCH = 512
RUN = os.environ.get("RUN", "run")
MIX = [("normal", 0.89), ("error", 0.04), ("slow", 0.03), ("very_slow", 0.01), ("slow_after", 0.01), ("swallowed", 0.02)]
SERVICES = ("lt-gateway", "lt-order")


def hexid(n: int) -> str:
    return "%0*x" % (n * 2, random.getrandbits(n * 8))


def attr(k, v):
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    return {"key": k, "value": {"stringValue": str(v)}}


def pick() -> str:
    r, acc = random.random(), 0.0
    for name, p in MIX:
        acc += p
        if r < acc:
            return name
    return MIX[0][0]


def make_trace(cat: str, t0: float) -> list[tuple[float, str, dict]]:
    """回傳 [(結束時間, 服務, span)]。時間是秒（epoch）。"""
    tid = hexid(16)
    db = {"normal": random.uniform(0.005, 0.04), "error": random.uniform(0.005, 0.04),
          "slow": random.uniform(1.5, 3.0), "very_slow": 12.0, "swallowed": random.uniform(0.005, 0.04),
          "slow_after": random.uniform(0.005, 0.04)}[cat]
    ids = [hexid(8) for _ in range(4)]
    # (服務, span id, parent, 名稱, kind, 開始, 結束)
    g0, g1 = t0, t0 + db + 0.008
    c0, c1 = t0 + 0.001, g1 - 0.001
    if cat == "slow_after":           # 呼叫下游很快就回來了，自己之後又做了 12 秒
        g1 = t0 + 12.0
    o0, o1 = c0 + 0.001, c1 - 0.001
    d0, d1 = o0 + 0.002, o0 + 0.002 + db
    code = 500 if cat == "error" else 200
    common = [attr("lt.category", cat), attr("lt.run", RUN)]

    def span(i, parent, name, kind, s, e, extra=(), err=False, events=()):
        sp = {"traceId": tid, "spanId": ids[i], "name": name, "kind": kind,
              "startTimeUnixNano": str(int(s * 1e9)), "endTimeUnixNano": str(int(e * 1e9)),
              "attributes": common + list(extra), "status": {"code": 2} if err else {}}
        if parent is not None:
            sp["parentSpanId"] = ids[parent]
        if events:
            sp["events"] = list(events)
        return sp

    exc = {"timeUnixNano": str(int(d1 * 1e9)), "name": "exception",
           "attributes": [attr("exception.type", "Npgsql.NpgsqlException"), attr("exception.message", "timeout")]}
    err = cat == "error"
    return [
        (g1, "lt-gateway", span(0, None, "GET /orders/{id}", 2, g0, g1,
                                [attr("http.request.method", "GET"), attr("http.route", "/orders/{id}"),
                                 attr("http.response.status_code", code)], err)),
        (c1, "lt-gateway", span(1, 0, "GET", 3, c0, c1, [attr("http.response.status_code", code)], err)),
        (o1, "lt-order", span(2, 1, "GET /orders/{id}", 2, o0, o1,
                              [attr("http.route", "/orders/{id}"), attr("http.response.status_code", code)], err)),
        (d1, "lt-order", span(3, 2, "SELECT orders", 3, d0, d1, [attr("db.system", "postgresql")], err,
                              [exc] if cat in ("error", "swallowed") else ())),
    ]


def post(service: str, spans: list[dict]) -> bool:
    body = {"resourceSpans": [{"resource": {"attributes": [attr("service.name", service)]},
                               "scopeSpans": [{"scope": {"name": "loadgen"}, "spans": spans}]}]}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        print(f"post failed: {e}", file=sys.stderr)
        return False


def main() -> None:
    sent = {name: 0 for name, _ in MIX}
    pending: list = []            # (結束時間, 序號, 服務, span)
    buf = {s: [] for s in SERVICES}
    next_flush = {s: time.time() + random.uniform(0, BATCH_DELAY) for s in SERVICES}
    start = last = time.time()
    carry, seq, spans_sent, fails = 0.0, 0, 0, 0
    report = start + 30
    while True:
        now = time.time()
        if now - start < DURATION:
            carry += RATE * (now - last)
            while carry >= 1:
                carry -= 1
                cat = pick()
                sent[cat] += 1
                for end, svc, sp in make_trace(cat, now):
                    seq += 1
                    heapq.heappush(pending, (end, seq, svc, sp))
        elif not pending and not any(buf.values()):
            break
        last = now
        while pending and pending[0][0] <= now:
            _, _, svc, sp = heapq.heappop(pending)
            buf[svc].append(sp)
        for svc in SERVICES:
            if buf[svc] and (now >= next_flush[svc] or len(buf[svc]) >= MAX_BATCH):
                chunk, buf[svc] = buf[svc][:MAX_BATCH], buf[svc][MAX_BATCH:]
                if post(svc, chunk):
                    spans_sent += len(chunk)
                else:
                    fails += len(chunk)
                if now >= next_flush[svc]:
                    next_flush[svc] = now + BATCH_DELAY
        if now >= report:
            print(f"{now - start:.0f}s：{sum(sent.values())} 個 trace、{spans_sent} 段已送、{len(pending)} 段還沒結束、送失敗 {fails} 段",
                  flush=True)
            report += 30
        time.sleep(0.02)
    print("SENT " + json.dumps({"run": RUN, "traces": sent, "spans": spans_sent, "failed_spans": fails,
                                "seconds": round(time.time() - start)}), flush=True)


if __name__ == "__main__":
    main()

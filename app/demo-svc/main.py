"""範例服務：沒有任何業務意義，只為了有東西可以監控、可以弄壞。

同一支程式用環境變數決定扮演哪個角色：

    ROLE=api    收到 /work 做一點假的運算後回應
    ROLE=web    收到 /work 後呼叫 UPSTREAM_URL（就是 api），並且自己定時打自己產生流量

吐出三種資料：
    metrics  :9464/metrics，Prometheus 格式
    log      stdout，一行一筆 JSON，帶 trace_id
    trace    只有編號：用 W3C 的 traceparent header 往下傳，還沒有送到任何地方

可以從外面弄壞（不用重新部署）：

    GET /admin/fault?latency_ms=800     每個請求多睡這麼久
    GET /admin/fault?error_rate=0.5     這個比例的請求回 500
    GET /admin/fault?cpu=1              開一條執行緒空轉吃 CPU（0 關掉）
    GET /admin/fault?mem_mb=64          多佔這麼多記憶體（0 放掉）
    GET /admin/fault?clear=1            全部恢復
    GET /admin/fault                    看現在的狀態
"""
import json
import os
import random
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from prometheus_client import Counter, Gauge, Histogram, start_http_server

ROLE = os.getenv("ROLE", "api")
SERVICE = os.getenv("SERVICE_NAME", "demo-" + ROLE)
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "9464"))
UPSTREAM_URL = os.getenv("UPSTREAM_URL", "")
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "5"))
SELF_TRAFFIC_RPS = float(os.getenv("SELF_TRAFFIC_RPS", "0"))

requests_total = Counter("demo_requests_total", "處理過的請求數", ["route", "code"])
duration = Histogram("demo_request_duration_seconds", "處理一個請求花多久", ["route"],
                     buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
inflight = Gauge("demo_inflight_requests", "正在處理中的請求數")
fault_gauge = Gauge("demo_fault", "現在開著的故障，0 = 沒開", ["kind"])

FAULT = {"latency_ms": 0.0, "error_rate": 0.0, "cpu": 0.0, "mem_mb": 0.0}
_lock = threading.Lock()
_ballast = []                 # mem_mb 佔住的記憶體
_burn = threading.Event()     # cpu 空轉的開關


def log(level, msg, **fields):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z", "level": level,
           "service": SERVICE, "msg": msg}
    rec.update(fields)
    sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def trace_ids(header):
    """從 traceparent 取出 trace_id；沒有就開一個新的。回傳 (trace_id, 要往下傳的 header)。"""
    parts = (header or "").split("-")
    trace_id = parts[1] if len(parts) == 4 and len(parts[1]) == 32 else secrets.token_hex(16)
    return trace_id, "00-%s-%s-01" % (trace_id, secrets.token_hex(8))


def burner():
    while True:
        _burn.wait()
        end = time.time() + 0.2
        while time.time() < end:
            pass


def set_fault(q):
    with _lock:
        if "clear" in q:
            for k in FAULT:
                FAULT[k] = 0.0
        for k in FAULT:
            if k in q:
                FAULT[k] = max(0.0, float(q[k][0]))
        if FAULT["cpu"] > 0:
            _burn.set()
        else:
            _burn.clear()
        _ballast.clear()
        if FAULT["mem_mb"] > 0:
            _ballast.append(bytearray(int(FAULT["mem_mb"]) * 1024 * 1024))
        for k, v in FAULT.items():
            fault_gauge.labels(kind=k).set(v)
        return dict(FAULT)


def do_work(traceparent):
    """回傳 (HTTP 狀態碼, 內容)。"""
    time.sleep(random.uniform(0.02, 0.06) + FAULT["latency_ms"] / 1000.0)
    if random.random() < FAULT["error_rate"]:
        return 500, {"error": "injected"}
    if not UPSTREAM_URL:
        return 200, {"service": SERVICE, "result": sum(i * i for i in range(2000))}
    req = urllib.request.Request(UPSTREAM_URL, headers={"traceparent": traceparent})
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return 200, {"service": SERVICE, "upstream": json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as e:
        return 502, {"error": "upstream returned %d" % e.code}
    except Exception as e:                      # 逾時、連不上
        return 504, {"error": "upstream unreachable: %s" % type(e).__name__}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):               # 內建的存取紀錄不是 JSON，關掉，自己印
        pass

    def reply(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/healthz", "/readyz"):
            return self.reply(200, {"ok": True})
        if url.path == "/admin/fault":
            state = set_fault(parse_qs(url.query))
            log("warn", "fault changed", **state)
            return self.reply(200, state)
        if url.path != "/work":
            return self.reply(404, {"error": "not found"})

        trace_id, child = trace_ids(self.headers.get("traceparent"))
        start = time.time()
        inflight.inc()
        try:
            code, body = do_work(child)
        finally:
            inflight.dec()
        took = time.time() - start
        requests_total.labels(route="/work", code=str(code)).inc()
        duration.labels(route="/work").observe(took)
        log("info" if code < 500 else "error", "handled", trace_id=trace_id, route="/work",
            code=code, duration_ms=round(took * 1000, 1))
        self.reply(code, body)


def self_traffic():
    """自己打自己，這樣不用另外產生流量就有資料。"""
    url = "http://127.0.0.1:%d/work" % HTTP_PORT
    while True:
        time.sleep(1.0 / SELF_TRAFFIC_RPS)
        try:
            urllib.request.urlopen(url, timeout=UPSTREAM_TIMEOUT + 2).read()
        except Exception:                       # 回 5xx 也算一次流量，已經記在 metrics 與 log 裡
            pass


def main():
    for k in FAULT:
        fault_gauge.labels(kind=k).set(0)
    start_http_server(METRICS_PORT)
    threading.Thread(target=burner, daemon=True).start()
    if SELF_TRAFFIC_RPS > 0:
        threading.Thread(target=self_traffic, daemon=True).start()
    log("info", "started", role=ROLE, http_port=HTTP_PORT, metrics_port=METRICS_PORT,
        upstream=UPSTREAM_URL or None, self_traffic_rps=SELF_TRAFFIC_RPS)
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

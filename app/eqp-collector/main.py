"""機台收值程式。

每隔一段時間向每台機台收一次值：
  - 收到的值、成功失敗、收值耗時 → Prometheus metrics（:9464/metrics），由 vmagent 抓去 VictoriaMetrics
  - 每一次收值的結果 → JSON log 寫到 stdout，由 Fluentd 收去 Elasticsearch

程式不需要知道 tenant、token、Elasticsearch 在哪。歸屬寫在 values.yaml 的 observability，
部署時變成 namespace 的 label，資料要送去哪由平台那邊依 label 決定。

目前 read_equipment() 是模擬的。接真的機台時只要換掉這一個函式，其他不用動。
"""
import json
import os
import random
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import Counter, Gauge, Histogram, start_http_server

EQUIPMENT = [e.strip() for e in os.getenv("EQUIPMENT", "EQP001,EQP002,EQP003").split(",") if e.strip()]
PARAMS = [p.strip() for p in os.getenv("PARAMS", "temperature,pressure,flow").split(",") if p.strip()]
INTERVAL = float(os.getenv("INTERVAL_SECONDS", "10"))
TIMEOUT = float(os.getenv("COLLECT_TIMEOUT_SECONDS", "3"))
FAIL_RATE = float(os.getenv("SIMULATED_FAIL_RATE", "0.03"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "9464"))
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))

# ── metrics ───────────────────────────────────────────────
# label 只放種類有限的東西（機台、參數、結果）。批號、工單號、trace_id 絕對不能當 label，
# 那會讓 series 數量無限增長，把儲存撐爆。那些要記就寫進 log。
collected = Counter("eqp_collect_total", "累計收值次數", ["machine", "result"])
value = Gauge("eqp_value", "機台最後一次收到的值", ["machine", "param"])
last_success = Gauge(
    "eqp_last_success_timestamp_seconds",
    "機台最後一次收值成功的時間。告警用：time() - 這個值 > 300 代表這台機台 5 分鐘沒收到值",
    ["machine"])
up = Gauge("eqp_up", "機台連線狀態，1 = 最近一次收值成功", ["machine"])
duration = Histogram("eqp_collect_duration_seconds", "單次收值耗時",
                     buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5))

# ── 健康檢查用的狀態 ──────────────────────────────────────
state = {"ready": False, "last_loop": time.time()}
stop = threading.Event()


def log(level, message, **fields):
    """一行一筆 JSON。level / message / trace_id 是 Fluentd 那邊認得的欄位，其他照樣保留。"""
    print(json.dumps({"level": level, "message": message, **fields}, ensure_ascii=False), flush=True)


def read_equipment(machine):
    """向一台機台收一次值，回傳 {param: value}。收不到就丟例外。

    模擬的：接真的機台時換掉這個函式就好（SECS/GEM、OPC UA、廠商 API 之類）。
    """
    time.sleep(random.uniform(0.02, 0.3))
    if random.random() < FAIL_RATE:
        raise TimeoutError("%s 在 %.0f 秒內沒有回應" % (machine, TIMEOUT))
    return {p: round(random.gauss(50, 5), 2) for p in PARAMS}


def collect_once():
    for machine in EQUIPMENT:
        trace_id = uuid.uuid4().hex[:16]
        started = time.time()
        try:
            readings = read_equipment(machine)
        except Exception as e:  # 一台失敗不影響其他台
            collected.labels(machine=machine, result="fail").inc()
            up.labels(machine=machine).set(0)
            log("WARN", "收值失敗", machine=machine, error=str(e), trace_id=trace_id)
            continue
        finally:
            duration.observe(time.time() - started)
        for param, v in readings.items():
            value.labels(machine=machine, param=param).set(v)
        collected.labels(machine=machine, result="ok").inc()
        up.labels(machine=machine).set(1)
        last_success.labels(machine=machine).set(time.time())
        log("INFO", "收值成功", machine=machine, readings=readings, trace_id=trace_id)


class Health(BaseHTTPRequestHandler):
    """/readyz：第一輪收完才 ready。/healthz：收值迴圈太久沒動就判定卡住，讓 kubelet 重啟。"""

    def do_GET(self):
        if self.path == "/readyz":
            ok = state["ready"]
        elif self.path == "/healthz":
            ok = time.time() - state["last_loop"] < INTERVAL * 3 + 30
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200 if ok else 503)
        self.end_headers()
        self.wfile.write(b"ok" if ok else b"not ok")

    def log_message(self, *args):  # 健康檢查每幾秒一次，不要洗版
        pass


def main():
    signal.signal(signal.SIGTERM, lambda *_: stop.set())  # 不處理的話 pod 要等滿寬限期才會停
    start_http_server(METRICS_PORT)
    threading.Thread(target=ThreadingHTTPServer(("", HTTP_PORT), Health).serve_forever, daemon=True).start()
    log("INFO", "開始收值", equipment=EQUIPMENT, params=PARAMS, interval=INTERVAL)

    while not stop.is_set():
        collect_once()
        state["ready"] = True
        state["last_loop"] = time.time()
        stop.wait(INTERVAL)

    log("INFO", "收到停止訊號，結束")
    return 0


if __name__ == "__main__":
    sys.exit(main())

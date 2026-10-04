"""持續模擬機台收值，吐成 Prometheus metrics 給 vmagent 抓。

用途是驗證分流：同一支程式部署到不同 namespace，資料會進哪些 tenant
完全由 namespace 上的 group label 決定。

程式本身不知道 tenant、group、token 的存在——這正是要驗證的事。
分流只發生在平台層那台 vmagent，應用什麼都不用改。
"""
import json
import os
import random
import sys
import time

from prometheus_client import Counter, Gauge, start_http_server

PORT = int(os.getenv("METRICS_PORT", "9464"))
SENSORS = [f"S{i:02d}" for i in range(1, int(os.getenv("SENSORS", "4")) + 1)]
INTERVAL = float(os.getenv("INTERVAL_SECONDS", "5"))
FAIL_RATE = float(os.getenv("FAIL_RATE", "0.05"))

# 固定為 1。用 count by (namespace) (sim_info) 就能數出每個 tenant 收到哪幾個模擬器。
info = Gauge("sim_info", "固定為 1，用來數各 tenant 收到哪些模擬器")

value = Gauge("sim_sensor_value", "感測值（隨機漫步）", ["sensor"])
samples = Counter("sim_samples_total", "累計收值筆數", ["sensor", "result"])
age = Gauge(
    "sim_last_sample_age_seconds",
    "距離這個感測器最後一筆成功收值過了幾秒，卡住時會直線上升",
    ["sensor"],
)

# label 只放種類有限的東西。批號、工單號這種高基數的絕對不能當 label。


def log(level, message, **fields):
    print(json.dumps({"level": level, "message": message, **fields}, ensure_ascii=False), flush=True)


def main():
    start_http_server(PORT)
    info.set(1)
    log("INFO", f"metrics 在 :{PORT}/metrics", sensors=len(SENSORS), interval=INTERVAL)

    current = {s: random.uniform(20, 80) for s in SENSORS}
    last_ok = {s: time.time() for s in SENSORS}

    while True:
        now = time.time()
        fails = 0
        for s in SENSORS:
            if random.random() < FAIL_RATE:
                samples.labels(sensor=s, result="fail").inc()
                fails += 1
            else:
                current[s] = min(100.0, max(0.0, current[s] + random.gauss(0, 1.5)))
                value.labels(sensor=s).set(round(current[s], 2))
                samples.labels(sensor=s, result="ok").inc()
                last_ok[s] = now
            age.labels(sensor=s).set(round(now - last_ok[s], 1))

        if fails:
            log("WARN", f"本輪有 {fails} 個感測器收值失敗", fails=fails)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

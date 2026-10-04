"""模擬 YMS：把「產線有沒有在動」吐成 metrics，把事件吐成 JSON log。

這支的重點不是模擬得多像，而是示範業務指標長什麼樣子：
機台多久沒回報、報工吞吐掉了沒、待處理積壓多少。這些是 CPU 和記憶體看不出來的。

刻意不做的事：不把量測值（良率、製程參數）寫進 metrics。
那是 SPC / FDC / YMS 自己的範圍，塞進來會因為高基數壓垮儲存。
"""
import json
import logging
import os
import random
import sys
import time
import uuid

from prometheus_client import Counter, Gauge, start_http_server

MACHINES = [f"EQP{i:03d}" for i in range(1, 9)]
METRICS_PORT = int(os.getenv("METRICS_PORT", "9464"))

# 最靈敏的訊號：卡住時這條直線上升
last_report_age = Gauge(
    "yms_machine_last_report_age_seconds",
    "距離這台機台最後一筆回報過了幾秒",
    ["machine_id"],
)
reports_total = Counter(
    "yms_reports_total", "累計報工筆數", ["machine_id", "result"]
)
pending_lots = Gauge("yms_pending_lots", "待處理批數")

# 基數警告：machine_id 只有幾十個沒問題，
# 但絕對不要把批號、工單號放進 label，那會把儲存撐爆。


class JsonFormatter(logging.Formatter):
    """輸出 Fluentd 那邊解析得了的格式：level / message / trace_id。"""

    def format(self, record):
        payload = {
            "level": record.levelname,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", ""),
        }
        return json.dumps(payload, ensure_ascii=False)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger("yms-sim")
log.addHandler(handler)
log.setLevel(logging.INFO)


def main():
    start_http_server(METRICS_PORT)
    log.info(f"metrics 在 :{METRICS_PORT}/metrics", extra={"trace_id": ""})

    last_seen = {m: time.time() for m in MACHINES}
    stuck = random.choice(MACHINES)  # 固定讓一台卡住，才看得出告警會不會響

    while True:
        now = time.time()
        for m in MACHINES:
            if m != stuck and random.random() < 0.7:
                last_seen[m] = now
                result = "ok" if random.random() < 0.97 else "fail"
                reports_total.labels(machine_id=m, result=result).inc()
                if result == "fail":
                    log.warning(
                        f"{m} 報工失敗，等待重試",
                        extra={"trace_id": uuid.uuid4().hex[:16]},
                    )
            last_report_age.labels(machine_id=m).set(now - last_seen[m])

        pending_lots.set(max(0, int(random.gauss(40, 12))))
        time.sleep(5)


if __name__ == "__main__":
    main()

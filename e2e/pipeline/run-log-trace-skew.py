# log 的時間比 span 早／晚 10 分鐘時，investigator 依 trace_id 撈 log（engine/investigator/es.py logs_of_traces）撈不撈得到
# （docs/pdca/phase1-collect/2026-10-log-trace-bind.md）。三筆同 trace_id 的 log 經 k8s-logs-otel 寫進 ES：
# 跟 span 同時、早 10 分鐘、晚 10 分鐘；用 investigator 的事故窗（± 5 分鐘）撈，三筆都要撈到。
# 不需要 K8s，只要連得到裝好 pipeline 的 ES（會寫進 k8s-it-c1）。
#
# 用法：ES_URL=http://<ip>:9200 ES_AUTH=elastic:<password> engine/.venv/Scripts/python e2e/pipeline/run-log-trace-skew.py
import datetime as dt, json, os, pathlib, sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "engine"))
from core.es import ES  # noqa: E402
from investigator import es as E  # noqa: E402

url, auth = os.environ["ES_URL"], tuple(os.environ["ES_AUTH"].split(":", 1))
T = os.urandom(16).hex()   # 每次新的 trace_id，重跑不會撈到上一次的
base = dt.datetime(2026, 10, 27, 1, 2, 3, tzinfo=dt.timezone.utc)   # span 的時間


def collector_doc(ts: str, msg: str) -> dict:
    """Collector 從容器 log 檔讀到的一行（跟 run-trace-id-cases.sh 同一個形狀）"""
    return {"@timestamp": ts, "body": {"text": json.dumps({"@timestamp": ts, "level": "INFO", "message": msg, "trace_id": T})},
            "attributes": {"log.iostream": "stdout", "log.file.path": "/var/log/pods/x/app/0.log"},
            "resource": {"attributes": {"service.name": "mes-api", "k8s.namespace.name": "mes-prod", "k8s.pod.name": "mes-api-7d9f-abcde",
                                        "k8s.pod.uid": "u1", "k8s.container.name": "app", "k8s.cluster.name": "c1", "dept": "D3120",
                                        "group": "it", "k8s.pod.labels.app.kubernetes.io/name": "mes-api"}}}


for off, msg in [(0, "same time"), (-10, "10 min early"), (10, "10 min late")]:
    ts = (base + dt.timedelta(minutes=off)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = requests.post(f"{url}/logs-generic.otel-it/_doc?pipeline=k8s-logs-otel&refresh=true", json=collector_doc(ts, msg), auth=auth)
    r.raise_for_status()

# investigator 的事故窗：前 2 分鐘到後 3 分鐘，再 ± 5 分鐘（investigator/__main__.py 的 pad）
start, end, pad = base - dt.timedelta(minutes=2), base + dt.timedelta(minutes=3), dt.timedelta(minutes=5)
got = sorted(l["message"] for l in E.logs_of_traces(ES(), "k8s-it-*", [T], start - pad, end + pad))
want = ["10 min early", "10 min late", "same time"]
print(f"{'PASS' if got == want else 'FAIL'}\t事故窗 {start:%H:%M}–{end:%H:%M}（± 5 分鐘）依 trace_id 撈到 {len(got)} 筆：{got}")
sys.exit(0 if got == want else 1)

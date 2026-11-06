"""分散模式（api＋beat＋worker＋Redis）的端到端：本機起 beat、三個 worker、api，對測試 ES 與一個 Redis 跑幾分鐘
（docs/pdca/meta/2026-11-api-beat-worker.md）。plugin 用 factory（有 ingest，才能驗 POST /api/anomalies）。

  ES_URL=http://localhost:19200 ES_AUTH=elastic:… AIOPS_BROKER_URL=redis://localhost:16379/0 python -X utf8 -m tests.distributed_e2e
約 4 分鐘。會在 aiops-anomalies、aiops-alerts 寫 SMOKE-01 這台機台的資料（每次先清掉）。

驗什麼：beat 每分鐘把 ingest、alert 丟進對的佇列（自己不跑）；realtime、alert 兩個 worker 真的跑了子命令；佇列不堆積（expires）；
POST /api/anomalies 要 token、寫進 ⑤、幾秒內 alert 開件（不用等下一分鐘）；POST /api/rca 讓那件重跑根因、標記拿掉；GET /api/alerts 看得到；
beat 收到 SIGTERM 乾淨結束。
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import redis

from shared.infra.es import ES

PY = sys.executable
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.environ.get("TEMP", "/tmp"), "aiops-distributed-e2e")
PORT = 18081
TOKEN = "smoke-token"
EQP = "SMOKE-01"
BROKER = os.environ.get("AIOPS_BROKER_URL", "redis://localhost:16379/0")
ENV = {**os.environ, "PYTHONUTF8": "1", "AIOPS_PLUGIN": "factory", "AIOPS_BROKER_URL": BROKER, "AIOPS_API_TOKEN": TOKEN,
       "API_PORT": str(PORT), "LLM_PROVIDER": "template", "TZ": "Asia/Taipei"}
fails: list[str] = []
procs: dict[str, subprocess.Popen] = {}


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"：{detail}" if detail and not cond else ""), flush=True)
    if not cond:
        fails.append(name)


def start(name, *args):
    os.makedirs(OUT, exist_ok=True)
    f = open(os.path.join(OUT, f"{name}.log"), "w", encoding="utf-8")
    procs[name] = subprocess.Popen([PY, "-X", "utf8", "-m", "main", *args], cwd=HERE, env=ENV, stdout=f, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return procs[name]


def log_of(name) -> str:
    with open(os.path.join(OUT, f"{name}.log"), encoding="utf-8", errors="replace") as f:
        return f.read()


def wait_for(pred, seconds, step=2):
    t0 = time.time()
    while time.time() - t0 < seconds:
        got = pred()
        if got:
            return got
        time.sleep(step)
    return pred()


def http(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def open_alert(es) -> dict | None:
    hits = es.search("aiops-alerts", {"size": 5, "query": {"bool": {"filter": [{"term": {"status": "open"}}, {"term": {"service": EQP}}]}}})["hits"]["hits"]
    return hits[0]["_source"] if hits else None


def main() -> int:
    es = ES()
    r = redis.Redis.from_url(BROKER)
    print(f"== 0. 清掉 {EQP} 的舊資料、清空佇列")
    for ix in ("aiops-anomalies", "aiops-alerts", "aiops-incidents"):
        es.req("POST", f"/{ix}/_delete_by_query?refresh=true&conflicts=proceed",
               {"query": {"bool": {"should": [{"term": {"service": EQP}}, {"term": {"keys.equipment": EQP}}], "minimum_should_match": 1}}}, ok=(200, 404))
    for q in ("realtime", "alert", "batch"):
        r.delete(q)

    try:
        print("== 1. 起 beat、三個 worker、api（跟 chart 的 distributed 一樣的指令）")
        start("beat", "scheduler", "--config", "configs/factory/schedule.yaml", "--stage", "2", "--enqueue", "--alive-file", os.path.join(OUT, "beat-alive"))
        for q, c in (("realtime", "2"), ("alert", "1"), ("batch", "1")):
            start(f"worker-{q}", "tasks", "worker", "--queue", q, "--concurrency", c)
        start("api", "api")
        check("api 起來了（/health）", wait_for(_health, 30))
        print("== 2. 等 beat 丟兩輪（約 2 分鐘）：工作進對的佇列、worker 真的跑了、佇列不堆積")
        beat = wait_for(lambda: ("[beat] alert" in log_of("beat") and "[beat] ingest" in log_of("beat")) and log_of("beat"), 150)
        check("beat 丟了 ingest、alert（自己不跑）", bool(beat) and "[scheduler] ingest" not in (beat or ""), (beat or "")[-300:])
        routes = {m.group(1): m.group(2) for m in re.finditer(r"\[beat\] (\S+) \S+ → (\S+)", beat or "")}
        check("  ingest → realtime、alert → alert（paths-merge 每 5 分鐘，有丟的話 → realtime）",
              routes.get("ingest") == "realtime" and routes.get("alert") == "alert" and routes.get("paths-merge", "realtime") == "realtime", str(routes))
        rt = wait_for(lambda: "[scheduler] ingest" in log_of("worker-realtime") and " ok " in log_of("worker-realtime") and log_of("worker-realtime"), 90)
        check("realtime worker 跑了 ingest（子行程、ok）", bool(rt), (rt or log_of("worker-realtime"))[-400:])
        al = wait_for(lambda: "[scheduler] alert" in log_of("worker-alert") and " ok " in log_of("worker-alert") and log_of("worker-alert"), 90)
        check("alert worker 跑了 alert（子行程、ok）", bool(al), (al or log_of("worker-alert"))[-400:])
        check("batch worker 活著、沒事做", procs["worker-batch"].poll() is None and "[scheduler]" not in log_of("worker-batch"))
        check("alert 不在 realtime worker 跑、ingest 不在 alert worker 跑", "[scheduler] alert" not in log_of("worker-realtime") and "[scheduler] ingest" not in log_of("worker-alert"))
        lens = {q: r.llen(q) for q in ("realtime", "alert", "batch")}
        check("佇列沒堆積（每個 ≤ 1）", all(v <= 1 for v in lens.values()), str(lens))

        print("== 3. POST /api/anomalies：要 token；寫進 ⑤；幾秒內開件（不等下一分鐘）")
        now = datetime.now(timezone.utc)
        rows = [{"@timestamp": (now - timedelta(minutes=2)).isoformat(), "is_anomaly": True, "equipment_id": EQP, "chamber_id": "CH-A",
                 "recipe_id": "R-SMOKE", "recipe_step": "3", "lot_id": "L-SMOKE", "parameter": "pressure", "value": 2.4, "expected": 1.2,
                 "score": 5.1, "run_id": f"smoke-{int(now.timestamp())}"}]
        code, body = http("POST", "/api/anomalies", {"input": "ml", "rows": rows})
        check("沒 token → 401", code == 401, str(body))
        t_post = time.time()
        code, body = http("POST", "/api/anomalies", {"input": "ml", "rows": rows}, {"Authorization": f"Bearer {TOKEN}"})
        check("有 token → 200、寫 1 筆、alert 已丟進佇列", code == 200 and body.get("written") == 1 and body.get("alert_queued") is True, str(body))
        got = es.search("aiops-anomalies", {"size": 1, "query": {"term": {"keys.equipment": EQP}}})["hits"]["hits"]
        check("  aiops-anomalies 有這筆（level ml、source ml）", bool(got) and got[0]["_source"].get("level") == "ml" and got[0]["_source"].get("source") == "ml",
              json.dumps(got[0]["_source"], ensure_ascii=False)[:200] if got else "沒有")
        alert = wait_for(lambda: open_alert(es), 45, step=1)
        dt = time.time() - t_post
        check(f"aiops-alerts 幾秒內開了 {EQP} 的件（{dt:.0f} 秒）", bool(alert), "45 秒內沒開件")
        check("  件有報告、沒有 rca_requested", bool(alert) and bool(alert.get("report")) and "rca_requested" not in alert)
        code, lst = http("GET", "/api/alerts?status=open&size=100")
        check("GET /api/alerts 看得到這件", code == 200 and any(a.get("alert_id") == (alert or {}).get("alert_id") for a in lst), str(lst)[:200])

        print("== 4. POST /api/rca：標在那件上 → 下一個 alert 強制重跑根因、標記拿掉")
        if alert:
            before = (alert.get("rca_attempts") or 1, alert.get("report_at"))
            code, body = http("POST", "/api/rca", {"alert_id": alert["alert_id"]})
            check("POST /api/rca → 200、queued", code == 200 and body.get("queued") is True, str(body))
            marked = es.req("GET", f"/aiops-alerts/_doc/{alert['alert_id']}").get("_source", {})
            rerun = wait_for(lambda: (a := open_alert(es)) and (a.get("rca_attempts") or 1) > before[0] and a, 60, step=2)
            check("alert 重跑了根因（rca_attempts +1、report_at 換新）", bool(rerun) and rerun.get("report_at") != before[1],
                  f"before={before} after={(rerun or {}).get('rca_attempts'), (rerun or {}).get('report_at')}")
            check("  標記 rca_requested 已拿掉", bool(rerun) and "rca_requested" not in rerun and "rca_requested_at" not in rerun)
            # 子行程的輸出要等它結束才 flush 進 log，ES 先看到結果再等 log
            seen = wait_for(lambda: "人要求重跑根因分析" in log_of("worker-alert"), 30)
            check("  alert worker 的 log 有「人要求重跑」", bool(seen), log_of("worker-alert")[-300:])
            code, body = http("POST", "/api/rca", {"alert_id": "a-no-such"})
            check("不存在的件 → 404", code == 404, str(body))

        print("== 5. 停 beat（SIGTERM）：乾淨結束")
        _stop("beat")
        check("beat 收到停止後結束、log 有「結束」", procs["beat"].returncode is not None and "結束" in log_of("beat"), log_of("beat")[-200:])
    finally:
        for n in list(procs):
            _stop(n)
    print(f"\n== 結果：{len(fails)} 個 FAIL" + (f"：{fails}" if fails else "") + f"（log 在 {OUT}）")
    return 1 if fails else 0


def _health() -> bool:
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3).status == 200
    except Exception:
        return False


def _stop(name):
    p = procs.get(name)
    if p is None or p.poll() is not None:
        return
    try:
        p.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM) if os.name == "nt" else signal.SIGTERM)
        p.wait(timeout=20)
    except Exception:
        p.kill()
        p.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())

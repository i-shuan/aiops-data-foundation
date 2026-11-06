"""用法（在 aiops-engine/code 底下）：

  python3 -m scheduler --config configs/schedule/schedule.yaml [--stage 1] [--alive-file /tmp/scheduler-alive]
      常駐：每分鐘照排程表跑該跑的工作。--stage 2 連第二階段的也開。
  python3 -m scheduler --config … --run train
      手動跑一次某個工作（含重試），跑完就結束；exit code 是成功與否。常駐的那個不知道這次（不會互擋），手動跑要自己確認沒在跑。
  python3 -m scheduler --config … --list
      列出會跑的工作與 cron。
  python3 -m scheduler --config … --enqueue
      beat（分散模式，tasks/）：每分鐘照排程表把該跑的工作丟進佇列（AIOPS_BROKER_URL），自己不跑；worker 另外起。只能 1 份。

SIGTERM／SIGINT：不再排新的，等還在跑的結束（pod 的 terminationGracePeriodSeconds 要夠長）。
"""
from __future__ import annotations

from aiops import CODE_ROOT

import argparse
import os
import signal
import sys
import threading
from datetime import datetime

from aiops.interface.jobs.runner import load_schedule, run_once, serve

HERE = str(CODE_ROOT)   # aiops-engine/code：子行程在這裡跑，configs/ 才找得到


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aiops.interface.jobs")
    p.add_argument("--config", default="configs/schedule/schedule.yaml")
    p.add_argument("--stage", type=int, default=int(os.environ.get("SCHEDULE_STAGE", "1")), help="開到第幾階段（預設 1；環境變數 SCHEDULE_STAGE）")
    p.add_argument("--alive-file", default=os.environ.get("SCHEDULE_ALIVE_FILE"), help="每分鐘更新一次的檔（liveness probe 用）")
    p.add_argument("--run", metavar="工作", help="手動跑一次這個工作就結束")
    p.add_argument("--list", action="store_true")
    p.add_argument("--enqueue", action="store_true", help="beat：只把工作丟進佇列（tasks/），不自己跑")
    a = p.parse_args(argv)
    tz, jobs = load_schedule(a.config, a.stage)
    if a.list:
        for j in jobs:
            print(f"{j.name:14} stage {j.stage}  {j.cron.expr:14} timeout {j.timeout:>5}s  retries {j.retries}  queue {j.queue:9} {' '.join(j.args)}")
        return 0
    if a.run:
        job = next((j for j in jobs if j.name == a.run), None)
        if job is None:
            print(f"沒有這個工作：{a.run}（有：{', '.join(j.name for j in jobs)}）", file=sys.stderr)
            return 2
        return 0 if run_once(job, datetime.now(tz), cwd=HERE) else 1
    run = None
    if a.enqueue:
        import aiops.interface.jobs.worker as tasks
        if not tasks.configured():
            print(f"--enqueue 要設 {tasks.BROKER_ENV}（佇列在哪）", file=sys.stderr)
            return 2
        run = tasks.enqueue
    stop = threading.Event()
    # SIGTERM：K8s 停 pod；SIGINT：Ctrl-C；SIGBREAK：Windows 上的 Ctrl-Break（本機跑分散模式的 e2e 用這個停它）
    for sig in (signal.SIGTERM, signal.SIGINT, *([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])):
        signal.signal(sig, lambda *_: stop.set())
    serve(jobs, tz, alive_file=a.alive_file, stop=stop, cwd=HERE, run=run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

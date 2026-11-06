"""佇列（Celery）：beat 把排程表的工作丟進來、api 把 alert 丟進來，worker 拿去跑（docs/pdca/meta/2026-11-api-beat-worker.md）。

角色跟 Superset 一樣：
  beat    python3 -m main scheduler --config … --enqueue        每分鐘照排程表丟工作，自己不跑；只能 1 份
  worker  python3 -m main tasks worker --queue realtime [-c 2]  從一個佇列拿工作跑；可以多份（alert 佇列固定 1 份、concurrency 1）
  api     python3 -m main api                                   POST /api/anomalies、/api/rca 後丟一個 alert 工作
佇列：realtime（每幾分鐘的）、alert（收斂，單一消費者）、batch（每天、每週的；吃資源）。工作本身就是「開子行程跑 python3 -m <args>」
（scheduler.runner.run_once），detector／investigator 的子命令一行不用改。
環境變數 AIOPS_BROKER_URL（redis://aiops-redis:6379/0）；沒設就是一隻 pod 的模式，enqueue／trigger 不做事、回 False。
"""
from __future__ import annotations

from shared.infra.env import CODE_ROOT

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from celery import Celery

from shared.scenario import load_scenario
from interface.jobs.cron import Cron
from interface.jobs.runner import Job, load_schedule, run_once

BROKER_ENV = "AIOPS_BROKER_URL"
QUEUES = ("realtime", "alert", "batch")
DEFAULT_QUEUE = "realtime"
TRIGGER_EXPIRES = 300            # api 丟的 alert：5 分鐘內沒被跑到就丟掉（beat 每分鐘還會丟新的）
HERE = str(CODE_ROOT)   # aiops-engine：子行程在這裡跑，configs/ 才找得到


def broker_url() -> str | None:
    return os.environ.get(BROKER_ENV) or None


def configured() -> bool:
    """有沒有佇列（分散模式）。沒有就是一隻 pod：排程器自己跑、api 只寫 ES 等下一分鐘。"""
    return broker_url() is not None


app = Celery("aiops", broker=broker_url() or "memory://")
app.conf.update(
    task_default_queue=DEFAULT_QUEUE,
    task_ignore_result=True,                 # 結果在 ES（aiops-*），不用 result backend
    task_acks_late=True,                     # worker 跑到一半被砍，工作回佇列再跑一次；子命令重跑都是覆蓋同一筆
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,            # 一次只拿一個：alert 佇列才真的同時只有一個在跑
    broker_connection_retry_on_startup=True,
    timezone=os.environ.get("TZ", "Asia/Taipei"),
    enable_utc=True,
)


@app.task(name="aiops.run_job", ignore_result=True)
def run_job(name: str, args: list[str], timeout: int = 300, retries: int = 0) -> bool:
    """跑一個排程表的工作：python3 -m <args>，逾時砍掉，失敗重試 retries 次。跟排程器自己跑完全一樣（scheduler.runner.run_once）。"""
    job = Job(name=name, cron=Cron("* * * * *"), args=[str(a) for a in args], timeout=int(timeout), retries=int(retries))
    return run_once(job, datetime.now(timezone.utc), cwd=HERE)


def expires_at(job, at: datetime) -> datetime | None:
    """這個工作到什麼時候沒跑就不用跑了：下一次排定前 5 秒（每分鐘的 55 秒、每 5 分鐘的 4 分 55 秒…）。
    等於排程器的「上一次沒跑完就跳過這次」：worker 忙到過期的工作直接丟掉，不會堆一排舊的 detect 追著跑。算不出下一次（8 天內沒有）回 None。"""
    nxt = job.cron.next_after(at)
    return nxt - timedelta(seconds=5) if nxt else None


def enqueue(job, at: datetime, expires: datetime | int | None = None) -> bool:
    """beat 用：把排程表的一個工作丟進它的佇列。沒設 broker 回 False（不丟）。"""
    if not configured():
        return False
    exp = expires if expires is not None else expires_at(job, at)
    run_job.apply_async(kwargs={"name": job.name, "args": list(job.args), "timeout": job.timeout, "retries": job.retries},
                        queue=job.queue, expires=exp)
    print(f"[beat] {job.name} {at.strftime('%Y-%m-%dT%H:%M%z')} → {job.queue}", flush=True)
    return True


def trigger(name: str, stage: int = 2) -> bool:
    """api 用：照這個情境的排程表丟一個叫 name 的工作（例如 alert）。沒設 broker、或排程表沒這個工作回 False。"""
    if not configured():
        return False
    _, jobs = load_schedule(os.path.join(HERE, load_scenario().schedule), stage)
    job = next((j for j in jobs if j.name == name), None)
    if job is None:
        return False
    return enqueue(job, datetime.now(timezone.utc), expires=TRIGGER_EXPIRES)


def worker_main(argv: list[str]) -> int:
    """python3 -m main tasks worker --queue realtime [--concurrency 2]：起一個只吃那個佇列的 worker。
    pool 用 threads：工作是等子行程，不吃 Python 的 CPU；Windows 也能跑。"""
    p = argparse.ArgumentParser(prog="tasks worker")
    p.add_argument("--queue", "-Q", default=DEFAULT_QUEUE, choices=QUEUES)
    p.add_argument("--concurrency", "-c", type=int, default=1)
    p.add_argument("--loglevel", default="info")
    a = p.parse_args(argv)
    if not configured():
        print(f"沒設 {BROKER_ENV}，worker 不知道佇列在哪", file=sys.stderr)
        return 2
    if a.queue == "alert" and a.concurrency != 1:
        print("alert 佇列只能 concurrency 1（收斂要單一消費者，不然兩筆異常同時進來會各開一件）", file=sys.stderr)
        return 2
    app.worker_main(["worker", "-Q", a.queue, "-c", str(a.concurrency), "--pool", "threads", "--loglevel", a.loglevel,
                     "--hostname", f"{a.queue}@%h", "--without-gossip", "--without-mingle"])
    return 0

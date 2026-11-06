"""排程表 → 每分鐘醒來一次，該跑的工作各開一條執行緒跑子行程（python3 -m <args>）。

跟 K8s CronJob 一樣的行為：
  同一個工作上一次沒跑完就跳過這次（Forbid）；超過 timeout 砍掉（activeDeadlineSeconds）；錯過的分鐘不補跑（只看醒來的那一分鐘）；
  失敗在同一次內重試 retries 次（backoffLimit），每幾分鐘的預設 0、每天／每週的預設 2。
每次執行結束在 stdout 留一行（[scheduler] <工作> <排定時間> <結果> <秒數>），子行程的輸出原樣印出（pod 的 log 就是全部的紀錄）。
分散模式（tasks/）：serve(run=tasks.enqueue) 時每分鐘只把該跑的工作丟進佇列（beat），不開子行程；每個工作的 queue 決定進哪個佇列。"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from shared.scenario.config import load_config


from interface.jobs.cron import Cron

QUEUES = ("realtime", "alert", "batch")


def default_queue(name: str, cron: Cron) -> str:
    """排程表沒寫 queue 時：alert 自己一個佇列（收斂要單一消費者）；每幾分鐘的進 realtime；每天、每週的進 batch（吃資源，跟即時的分開）。"""
    if name == "alert":
        return "alert"
    return "realtime" if cron.frequent else "batch"


@dataclass
class Job:
    name: str
    cron: Cron
    args: list[str]
    timeout: int
    retries: int
    stage: int = 1
    why: str = ""
    queue: str = "realtime"
    running: threading.Thread | None = field(default=None, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        cron = Cron(str(d["cron"]))
        retries = d.get("retries")
        if retries is None:
            retries = 0 if cron.every_minute else 2   # 每幾分鐘的：下一輪就是重試；每天、每週的：不重試要等明天
        queue = str(d.get("queue") or default_queue(str(d["name"]), cron))
        if queue not in QUEUES:
            raise ValueError(f"工作 {d['name']} 的 queue 要是 {'／'.join(QUEUES)} 之一，不是 {queue!r}")
        return cls(name=str(d["name"]), cron=cron, args=[str(a) for a in d["args"]], timeout=int(d.get("timeout", 300)),
                   retries=int(retries), stage=int(d.get("stage", 1)), why=str(d.get("why", "")), queue=queue)


def load_schedule(path: str, stage: int) -> tuple[ZoneInfo, list[Job]]:
    """stage：開到第幾階段（1 只開 stage 1 的；2 全開）。排程表先過 core/config 的驗證（鍵打錯在這裡就擋）。"""
    cfg = load_config(path, "schedule")
    tz = ZoneInfo(str(cfg.get("timezone", "UTC")))
    jobs = [Job.from_dict(d) for d in cfg.get("jobs", [])]
    names = [j.name for j in jobs]
    if len(set(names)) != len(names):
        raise ValueError(f"schedule 的工作名稱重複：{names}")
    return tz, [j for j in jobs if j.stage <= stage]


def log(msg: str) -> None:
    print(f"[scheduler] {msg}", flush=True)


def run_once(job: Job, at: datetime, python: str = sys.executable, cwd: str | None = None) -> bool:
    """跑一次（含重試）；回傳成功與否。子行程 stdout／stderr 原樣往下送。"""
    label = at.strftime("%Y-%m-%dT%H:%M%z")
    for attempt in range(job.retries + 1):
        t0 = time.monotonic()
        try:
            rc = subprocess.run([python, "-m", *job.args], timeout=job.timeout, cwd=cwd).returncode
        except subprocess.TimeoutExpired:
            log(f"{job.name} {label} 逾時 {job.timeout} 秒，已砍{'' if attempt == job.retries else '，重試'}")
            continue
        secs = time.monotonic() - t0
        if rc == 0:
            log(f"{job.name} {label} ok {secs:.1f}s" + (f"（第 {attempt + 1} 次）" if attempt else ""))
            return True
        log(f"{job.name} {label} 失敗 rc={rc} {secs:.1f}s" + ("，重試" if attempt < job.retries else f"（已重試 {job.retries} 次，放棄）" if job.retries else ""))
    return False


def tick(jobs: list[Job], at: datetime, python: str = sys.executable, cwd: str | None = None,
         run: Callable[[Job, datetime], object] | None = None) -> list[str]:
    """這一分鐘該跑的工作各開一條執行緒；上一次還在跑的跳過。回傳有開始跑的工作名稱。
    run 給了（beat 模式：tasks.enqueue）就直接呼叫它、不開執行緒、不看有沒有在跑——丟進佇列很快，重不重疊由佇列的 expires 管。"""
    started = []
    for job in jobs:
        if not job.cron.matches(at):
            continue
        if run is not None:
            run(job, at)
            started.append(job.name)
            continue
        if job.running is not None and job.running.is_alive():
            log(f"{job.name} {at.strftime('%Y-%m-%dT%H:%M%z')} 跳過：上一次還在跑")
            continue
        t = threading.Thread(target=run_once, args=(job, at, python, cwd), name=f"job-{job.name}", daemon=True)
        job.running = t
        t.start()
        started.append(job.name)
    return started


def touch(path: str | None) -> None:
    if path:
        with open(path, "w") as f:
            f.write(str(int(time.time())))


def serve(jobs: list[Job], tz: ZoneInfo, alive_file: str | None = None, stop: threading.Event | None = None,
          python: str = sys.executable, cwd: str | None = None, run: Callable[[Job, datetime], object] | None = None) -> None:
    """每到整分跑一次 tick；alive_file 每次醒來更新（liveness probe 看它）。stop 設了就不再排新的，等還在跑的收尾。
    run 給了就是 beat：只丟進佇列（tick 的 run）。"""
    stop = stop or threading.Event()
    log(f"開始：{len(jobs)} 個工作（{', '.join(j.name for j in jobs)}），時區 {tz.key}" + ("，只丟進佇列（beat）" if run else ""))
    touch(alive_file)
    while not stop.is_set():
        now = datetime.now(tz)
        nxt = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        # 一秒醒一次看有沒有被叫停：Windows 的 Event.wait 不會被訊號打斷，一次等 60 秒會晚一分鐘才停；Linux 不差
        while not stop.is_set() and (nxt - datetime.now(tz)).total_seconds() > 0:
            stop.wait(min((nxt - datetime.now(tz)).total_seconds(), 1))
        if stop.is_set():
            break
        touch(alive_file)
        at = datetime.now(tz).replace(second=0, microsecond=0)
        if at < nxt:          # 醒早了幾毫秒
            at = nxt
        tick(jobs, at, python, cwd, run)
    log("收到停止：不再排新的，等還在跑的結束")
    for j in jobs:
        if j.running is not None and j.running.is_alive():
            j.running.join(timeout=j.timeout)
    log("結束")

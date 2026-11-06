"""佇列（tasks/）與 beat 模式（docs/pdca/meta/2026-11-api-beat-worker.md）：工作進哪個佇列、過期時間＝下一次排定前、beat 只丟不跑、
worker 的任務就是跑子行程、api 的 trigger 照排程表丟 alert。不連 Redis：apply_async 用 mock 接住。"""
import glob
import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import aiops.interface.jobs.worker as tasks
from aiops.interface.jobs.cron import Cron
from aiops.interface.jobs.runner import QUEUES, Job, default_queue, load_schedule, tick

HERE = os.path.dirname(os.path.abspath(__file__))
TPE = ZoneInfo("Asia/Taipei")
BROKER = {tasks.BROKER_ENV: "redis://fake:6379/0"}


def at(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TPE)


def job(name="j", cron="* * * * *", queue=None, args=("tests._run_code", "pass"), timeout=5, retries=0):
    d = {"name": name, "cron": cron, "args": list(args), "timeout": timeout, "retries": retries}
    if queue:
        d["queue"] = queue
    return Job.from_dict(d)


class QueueOfJob(unittest.TestCase):
    """排程表沒寫 queue 時：alert 自己一個；每幾分鐘的 realtime；每天、每週的 batch。寫了就照寫的；寫錯報錯。"""

    def test_default_rule(self):
        self.assertEqual(default_queue("alert", Cron("* * * * *")), "alert")
        self.assertEqual(default_queue("detect", Cron("* * * * *")), "realtime")
        self.assertEqual(default_queue("peer", Cron("*/5 * * * *")), "realtime")
        self.assertEqual(default_queue("x", Cron("0,30 * * * *")), "realtime")
        self.assertEqual(default_queue("train", Cron("0 2 * * *")), "batch")
        self.assertEqual(default_queue("chronic", Cron("0 8 * * 1")), "batch")

    def test_explicit_and_bad(self):
        self.assertEqual(job("train", "0 2 * * *").queue, "batch")
        self.assertEqual(job("train", "0 2 * * *", queue="realtime").queue, "realtime")
        with self.assertRaises(ValueError):
            job("x", queue="fast")

    def test_every_builtin_schedule_routes_alert_alone(self):
        """四張排程表：每個工作的 queue 都合法、alert 一定在 alert 佇列、每分鐘的不在 batch。"""
        files = glob.glob(os.path.join(HERE, "..", "configs", "*", "schedule.yaml"))
        self.assertGreaterEqual(len(files), 4)
        for f in files:
            _, jobs = load_schedule(f, stage=2)
            for j in jobs:
                self.assertIn(j.queue, QUEUES, (f, j.name))
                if j.name == "alert":
                    self.assertEqual(j.queue, "alert", f)
                if j.cron.every_minute and j.name != "alert":
                    self.assertEqual(j.queue, "realtime", (f, j.name))


class NextRunAndExpiry(unittest.TestCase):
    """過期時間＝下一次排定前 5 秒：worker 忙到那時候的工作直接丟掉（等於排程器的「上一次沒跑完就跳過」）。"""

    def test_next_after(self):
        self.assertEqual(Cron("* * * * *").next_after(at(2026, 10, 31, 9, 7, 30)), at(2026, 10, 31, 9, 8))
        self.assertEqual(Cron("*/5 * * * *").next_after(at(2026, 10, 31, 9, 7)), at(2026, 10, 31, 9, 10))
        self.assertEqual(Cron("*/5 * * * *").next_after(at(2026, 10, 31, 9, 10)), at(2026, 10, 31, 9, 15))   # 不含 t 那一分鐘
        self.assertEqual(Cron("0 2 * * *").next_after(at(2026, 10, 31, 9, 0)), at(2026, 11, 1, 2, 0))
        self.assertEqual(Cron("0 8 * * 1").next_after(at(2026, 11, 1, 8, 0)), at(2026, 11, 2, 8, 0))      # 週日 → 週一
        self.assertIsNone(Cron("0 0 31 2 *").next_after(at(2026, 1, 1, 0, 0)))                            # 2 月 31 日：永遠沒有

    def test_expires_at(self):
        self.assertEqual(tasks.expires_at(job(cron="* * * * *"), at(2026, 10, 31, 9, 7)), at(2026, 10, 31, 9, 7, 55))
        self.assertEqual(tasks.expires_at(job(cron="*/5 * * * *"), at(2026, 10, 31, 9, 5)), at(2026, 10, 31, 9, 9, 55))
        self.assertIsNone(tasks.expires_at(job(cron="0 0 31 2 *"), at(2026, 1, 1, 0, 0)))


class Enqueue(unittest.TestCase):
    def test_without_broker_does_nothing(self):
        with mock.patch.dict(os.environ, {tasks.BROKER_ENV: ""}), mock.patch.object(tasks.run_job, "apply_async") as aa:
            self.assertFalse(tasks.configured())
            self.assertFalse(tasks.enqueue(job(), at(2026, 10, 31, 9, 7)))
            self.assertFalse(tasks.trigger("alert"))
            aa.assert_not_called()

    def test_enqueue_goes_to_the_jobs_queue_with_expiry(self):
        j = job("train", "0 2 * * *", args=("aiops", "detector", "train", "--config", "x.yaml"), timeout=3600, retries=2)
        with mock.patch.dict(os.environ, BROKER), mock.patch.object(tasks.run_job, "apply_async") as aa, redirect_stdout(io.StringIO()) as out:
            self.assertTrue(tasks.enqueue(j, at(2026, 10, 31, 2, 0)))
        kw = aa.call_args.kwargs
        self.assertEqual(kw["queue"], "batch")
        self.assertEqual(kw["expires"], at(2026, 11, 1, 1, 59, 55))
        self.assertEqual(kw["kwargs"], {"name": "train", "args": ["aiops", "detector", "train", "--config", "x.yaml"], "timeout": 3600, "retries": 2})
        self.assertIn("[beat] train", out.getvalue())

    def test_beat_tick_only_enqueues(self):
        """tick 給了 run 就只呼叫它：不開執行緒、不管上一次有沒有在跑、不符合 cron 的不丟。"""
        jobs = [job("a", "* * * * *"), job("b", "*/5 * * * *"), job("c", "0 2 * * *")]
        got = []
        with mock.patch("aiops.interface.jobs.runner.threading.Thread") as th:
            names = tick(jobs, at(2026, 10, 31, 9, 7), run=lambda j, t: got.append((j.name, t)))
        self.assertEqual(names, ["a"])
        self.assertEqual(got, [("a", at(2026, 10, 31, 9, 7))])
        th.assert_not_called()
        self.assertEqual(tick(jobs, at(2026, 10, 31, 9, 10), run=lambda j, t: got.append(j.name)), ["a", "b"])

    def test_trigger_finds_alert_in_plugin_schedule(self):
        """api 丟 alert：照這個 plugin 的排程表（預設 observability → configs/schedule/schedule.yaml）找 alert 工作，進 alert 佇列，5 分鐘過期。"""
        with mock.patch.dict(os.environ, {**BROKER, "AIOPS_PLUGIN": "observability"}), \
             mock.patch.object(tasks.run_job, "apply_async") as aa, redirect_stdout(io.StringIO()):
            self.assertTrue(tasks.trigger("alert"))
            self.assertFalse(tasks.trigger("no-such-job"))
        kw = aa.call_args.kwargs
        self.assertEqual(kw["queue"], "alert")
        self.assertEqual(kw["expires"], tasks.TRIGGER_EXPIRES)
        self.assertEqual(kw["kwargs"]["args"][:3], ["aiops", "investigator", "alert"])


class RunJobTask(unittest.TestCase):
    """worker 的任務＝跑子行程（跟排程器自己跑一樣）：成功回 True、失敗回 False。"""

    def test_runs_subprocess(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertTrue(tasks.run_job("ok", ["tests._run_code", "pass"], timeout=20))
            self.assertFalse(tasks.run_job("bad", ["tests._run_code", "raise SystemExit(3)"], timeout=20))
        self.assertIn("ok", out.getvalue())
        self.assertIn("失敗 rc=3", out.getvalue())


class WorkerMain(unittest.TestCase):
    def test_alert_queue_must_be_single_consumer(self):
        with mock.patch.dict(os.environ, BROKER), mock.patch.object(tasks.app, "worker_main") as wm:
            self.assertEqual(tasks.worker_main(["--queue", "alert", "--concurrency", "2"]), 2)
            wm.assert_not_called()
            self.assertEqual(tasks.worker_main(["--queue", "alert"]), 0)
            self.assertEqual(wm.call_args[0][0][:5], ["worker", "-Q", "alert", "-c", "1"])
            self.assertEqual(tasks.worker_main(["--queue", "realtime", "-c", "3"]), 0)
            self.assertEqual(wm.call_args[0][0][:5], ["worker", "-Q", "realtime", "-c", "3"])

    def test_without_broker(self):
        with mock.patch.dict(os.environ, {tasks.BROKER_ENV: ""}), mock.patch.object(tasks.app, "worker_main") as wm:
            self.assertEqual(tasks.worker_main(["--queue", "realtime"]), 2)
            wm.assert_not_called()

"""同一個工作同一時間只能有一份在跑（common/infra/pg.job_lock、interface/jobs/runner.run_once；docs/pdca/meta/2026-11-job-lock.md）。
對真的 PostgreSQL（tests/pg.py）跑。為了確認：
1. 兩個排程器（或 worker）同時跑同一個工作：只有一個真的跑，另一個印「跳過」、不開子行程。兩個「排程器」用兩個獨立的行程。
2. 不同工作互不影響；跑完鎖放掉，下一輪照常跑。
3. 拿著鎖的行程被 kill -9：鎖自動放掉，不會卡住之後每一輪。
4. 連不到 PostgreSQL：這輪不跑（不知道別人有沒有在跑）、回失敗，子行程沒開。
5. 沒設 AIOPS_PG_DSN（本機沒有資料庫）：不鎖、照跑。"""
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from common.infra.pg import job_lock
from interface.jobs.cron import Cron
from interface.jobs.runner import Job, run_once
from tests.pg import fresh_dsn, needs_pg

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
AT = datetime(2026, 11, 7, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def job(name, code):
    return Job(name=name, cron=Cron("* * * * *"), args=["tests._run_code", code], timeout=30, retries=0)


# 另一個「排程器」：獨立的行程，跑 run_once 一次，印出 log
OTHER = """
import sys; sys.path[:0] = ["src", "."]
from datetime import datetime; from zoneinfo import ZoneInfo
from interface.jobs.cron import Cron; from interface.jobs.runner import Job, run_once
j = Job(name=sys.argv[1], cron=Cron("* * * * *"), args=["tests._run_code", sys.argv[2]], timeout=30, retries=0)
run_once(j, datetime(2026, 11, 7, 9, 0, tzinfo=ZoneInfo("Asia/Taipei")), cwd=".")
"""


@needs_pg
class JobLock(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"AIOPS_PG_DSN": fresh_dsn()})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.marks = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(__import__("shutil").rmtree, self.marks, True)

    def code(self, tag, secs):
        """子行程：記下自己跑了（檔案），睡 secs 秒。"""
        return f"open({os.path.join(self.marks, tag)!r}, 'a').write('x'); import time; time.sleep({secs})"

    def ran(self):
        return sorted(os.listdir(self.marks))

    def other(self, name, code):
        return subprocess.Popen([sys.executable, "-c", OTHER, name, code], cwd=ROOT, env=os.environ.copy(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def test_two_schedulers_same_job_only_one_runs(self):
        p = self.other("alert", self.code("other", 3))                 # 另一個排程器先跑、拿著鎖 3 秒
        time.sleep(1.0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = run_once(job("alert", self.code("me", 0)), AT, cwd=ROOT)
        out_other, _ = p.communicate(timeout=30)
        self.assertTrue(ok)                                             # 跳過不算失敗
        self.assertIn("跳過：別的排程器或 worker 正在跑", buf.getvalue())
        self.assertIn("alert 2026-11-07T09:00+0800 ok", out_other)
        self.assertEqual(self.ran(), ["other"])                         # 只有一個真的跑了

    def test_other_jobs_not_blocked_and_lock_released_after(self):
        p = self.other("train", self.code("train", 2))
        time.sleep(1.0)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(run_once(job("detect", self.code("detect", 0)), AT, cwd=ROOT))   # 別的工作照跑
        p.communicate(timeout=30)
        with redirect_stdout(io.StringIO()) as buf:
            self.assertTrue(run_once(job("train", self.code("train-again", 0)), AT, cwd=ROOT))   # 跑完放掉了
        self.assertNotIn("跳過", buf.getvalue())
        self.assertEqual(self.ran(), ["detect", "train", "train-again"])

    def test_killed_holder_releases_lock(self):
        p = self.other("train", self.code("train", 60))
        time.sleep(1.5)
        with job_lock("train") as got:
            self.assertFalse(got)                                       # 還拿著
        os.kill(p.pid, signal.SIGKILL)                                  # 排程器被 OOM／kill -9（子行程留著也沒關係，鎖在排程器的連線上）
        p.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline:
            with job_lock("train") as got:
                if got:
                    break
            time.sleep(0.2)
        self.assertTrue(got)

    def test_db_unreachable_does_not_run(self):
        with mock.patch.dict(os.environ, {"AIOPS_PG_DSN": "postgresql://x@127.0.0.1:1/none", "AIOPS_PG_CONNECT_TIMEOUT": "2"}):
            with redirect_stdout(io.StringIO()) as buf:
                ok = run_once(job("alert", self.code("me", 0)), AT, cwd=ROOT)
        self.assertFalse(ok)
        self.assertIn("沒跑：連不到 PostgreSQL", buf.getvalue())
        self.assertEqual(self.ran(), [])


class NoDatabase(unittest.TestCase):
    def test_without_dsn_no_lock(self):
        with mock.patch.dict(os.environ, {"AIOPS_PG_DSN": ""}):
            with job_lock("x") as a, job_lock("x") as b:
                self.assertEqual((a, b), (True, True))


if __name__ == "__main__":
    unittest.main()

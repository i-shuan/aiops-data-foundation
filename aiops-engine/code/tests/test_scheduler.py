"""scheduler：cron 比對、不重疊、逾時砍掉、重試、stage、排程表本身（docs/pdca/phase3-locate/2026-10-single-pod-scheduler.md）。
子行程用 python -c 代替真的指令，不連 ES。"""
import io
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from zoneinfo import ZoneInfo

from scheduler.cron import Cron
from scheduler.runner import Job, load_schedule, run_once, tick

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEDULE = os.path.join(HERE, "..", "configs", "schedule", "schedule.yaml")
TPE = ZoneInfo("Asia/Taipei")


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=TPE)


def job(name="j", code="pass", timeout=5, retries=0, cron="* * * * *"):
    # runner 固定跑 python -m <args>：用 tests._run_code 當模組，把要跑的小程式當參數
    return Job(name=name, cron=Cron(cron), args=["tests._run_code", code], timeout=timeout, retries=retries)


class CronTest(unittest.TestCase):
    def test_every_minute_and_every_5(self):
        self.assertTrue(Cron("* * * * *").matches(at(2026, 10, 31, 9, 7)))
        c = Cron("*/5 * * * *")
        self.assertTrue(c.matches(at(2026, 10, 31, 9, 5)))
        self.assertFalse(c.matches(at(2026, 10, 31, 9, 7)))
        self.assertTrue(c.every_minute is False)

    def test_daily_weekly(self):
        self.assertTrue(Cron("0 2 * * *").matches(at(2026, 10, 31, 2, 0)))
        self.assertFalse(Cron("0 2 * * *").matches(at(2026, 10, 31, 2, 1)))
        mon = Cron("0 8 * * 1")
        self.assertTrue(mon.matches(at(2026, 11, 2, 8, 0)))      # 2026-11-02 是星期一
        self.assertFalse(mon.matches(at(2026, 11, 1, 8, 0)))     # 星期日
        self.assertTrue(Cron("0 8 * * 7").matches(at(2026, 11, 1, 8, 0)))   # 7 也是星期日
        self.assertTrue(Cron("0 8 * * 0").matches(at(2026, 11, 1, 8, 0)))

    def test_lists_ranges_steps(self):
        c = Cron("1,31 9-17 * 1-6/2 1-5")
        self.assertTrue(c.matches(at(2026, 3, 3, 9, 31)))         # 3 月、星期二
        self.assertFalse(c.matches(at(2026, 4, 1, 9, 31)))        # 4 月不在 1-6/2
        self.assertFalse(c.matches(at(2026, 3, 7, 9, 31)))        # 星期六

    def test_bad_expr(self):
        for bad in ("* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "5-3 * * * *"):
            with self.assertRaises(ValueError, msg=bad):
                Cron(bad)


class RunnerTest(unittest.TestCase):
    def run_capture(self, fn, *a, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            r = fn(*a, **kw)
        return r, buf.getvalue()

    def test_ok_and_failure_logged(self):
        ok, out = self.run_capture(run_once, job(code="pass"), at(2026, 10, 31, 9, 0), cwd=os.path.join(HERE, ".."))
        self.assertTrue(ok)
        self.assertIn("[scheduler] j 2026-10-31T09:00+0800 ok", out)
        ok, out = self.run_capture(run_once, job(code="raise SystemExit(3)"), at(2026, 10, 31, 9, 0), cwd=os.path.join(HERE, ".."))
        self.assertFalse(ok)
        self.assertIn("失敗 rc=3", out)
        self.assertNotIn("重試", out)      # retries 0：不重試

    def test_retries_until_success(self):
        # 第 1、2 次失敗、第 3 次成功：用檔案當計數器
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "n").replace("\\", "/")
            code = f"import os; n=int(open('{f}').read()) if os.path.exists('{f}') else 0; open('{f}','w').write(str(n+1)); raise SystemExit(0 if n>=2 else 1)"
            ok, out = self.run_capture(run_once, job(code=code, retries=2), at(2026, 10, 31, 2, 0), cwd=os.path.join(HERE, ".."))
            self.assertTrue(ok)
            self.assertEqual(out.count("失敗 rc=1"), 2)
            self.assertIn("ok", out)
            self.assertIn("（第 3 次）", out)
            ok, out = self.run_capture(run_once, job(code="raise SystemExit(1)", retries=2), at(2026, 10, 31, 2, 0), cwd=os.path.join(HERE, ".."))
            self.assertFalse(ok)
            self.assertEqual(out.count("失敗 rc=1"), 3)
            self.assertIn("已重試 2 次，放棄", out)

    def test_timeout_kills(self):
        t0 = time.monotonic()
        ok, out = self.run_capture(run_once, job(code="import time; time.sleep(30)", timeout=1), at(2026, 10, 31, 9, 0), cwd=os.path.join(HERE, ".."))
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIn("逾時 1 秒，已砍", out)

    def test_overlap_is_skipped_and_others_still_run(self):
        slow = job("slow", code="import time; time.sleep(3)", timeout=10)
        fast = job("fast", code="pass")
        buf = io.StringIO()
        with redirect_stdout(buf):
            started = tick([slow, fast], at(2026, 10, 31, 9, 0), cwd=os.path.join(HERE, ".."))
            self.assertEqual(started, ["slow", "fast"])
            time.sleep(1)
            started2 = tick([slow, fast], at(2026, 10, 31, 9, 1), cwd=os.path.join(HERE, ".."))   # slow 還在跑：跳過；fast 照跑
            self.assertEqual(started2, ["fast"])
            for j in (slow, fast):
                j.running.join(timeout=15)
        out = buf.getvalue()
        self.assertIn("slow 2026-10-31T09:01+0800 跳過：上一次還在跑", out)
        self.assertEqual(out.count("fast 2026-10-31T09:0"), 2)
        self.assertIn("slow 2026-10-31T09:00+0800 ok", out)

    def test_tick_only_runs_matching_cron(self):
        daily = job("daily", cron="0 2 * * *")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(tick([daily], at(2026, 10, 31, 9, 0), cwd=os.path.join(HERE, "..")), [])
            self.assertEqual(tick([daily], at(2026, 10, 31, 2, 0), cwd=os.path.join(HERE, "..")), ["daily"])
            daily.running.join(timeout=15)


class ScheduleFileTest(unittest.TestCase):
    """configs/schedule/schedule.yaml 本身：跟原本 CronJob 的設計一致（docs/pdca/phase3-locate/2026-10-schedule.md）。"""

    def test_stage1_is_the_minimal_loop(self):
        tz, jobs = load_schedule(SCHEDULE, 1)
        self.assertEqual(tz.key, "Asia/Taipei")
        self.assertEqual(sorted(j.name for j in jobs), ["alert", "detect", "rules", "trace-merge", "train"])
        _, all_jobs = load_schedule(SCHEDULE, 2)
        self.assertEqual(len(all_jobs), 13)

    def test_alert_timeout_covers_llm_calls(self):
        # 一件新的：LLM 最多 60 秒逾時（core/llm.py）；一輪至少要能處理好幾件，不然被砍什麼都沒寫
        import inspect
        from core import llm as LLM
        self.assertIn("timeout=60", inspect.getsource(LLM._call_openai))
        _, jobs = load_schedule(SCHEDULE, 2)
        by = {j.name: j for j in jobs}
        self.assertGreaterEqual(by["alert"].timeout, 5 * 60 + 60)

    def test_retries_follow_frequency(self):
        # 每幾分鐘的：下一輪就是重試；每天、每週的：不重試就要等明天（train 失敗 → baseline 舊一天）
        _, jobs = load_schedule(SCHEDULE, 2)
        for j in jobs:
            self.assertEqual(j.retries, 0 if j.cron.every_minute else 2, j.name)
        by = {j.name: j for j in jobs}
        self.assertEqual((by["train"].retries, by["rules"].retries, by["peer"].retries), (2, 0, 2))

    def test_every_job_has_timeout_and_runs_a_real_command(self):
        _, jobs = load_schedule(SCHEDULE, 2)
        for j in jobs:
            self.assertGreater(j.timeout, 0, j.name)
            self.assertIn(j.args[0], ("detector", "investigator"), j.name)
            cfg = j.args[j.args.index("--config") + 1]
            self.assertTrue(os.path.isfile(os.path.join(HERE, "..", cfg)), f"{j.name}：{cfg} 不存在")


if __name__ == "__main__":
    unittest.main()

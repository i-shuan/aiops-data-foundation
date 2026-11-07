"""排程器的看守（interface/jobs/guard.py；docs/pdca/meta/2026-11-pg-resilience.md）。為了確認：
1. 資料庫連不上：不到 AIOPS_FALLBACK_AFTER_MINUTES 不吵；到了打一次 webhook；之後每 60 分鐘才再提醒；恢復時通知一次。
2. 連得上：每分鐘打外部看門狗；連不上時不打（外部服務就會發現 engine 有問題）。
3. 連得上時補寫本機日誌裡別的程式留下的。
4. 看門狗、webhook 自己壞了只記一行，不影響之後。"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from interface.jobs.guard import Guard

T0 = datetime(2026, 11, 7, 2, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)
ENV = {"AIOPS_PG_DSN": "postgresql://x@db/aiops", "AIOPS_FALLBACK_WEBHOOK": "https://hook/x", "AIOPS_WATCHDOG_URL": "https://dog/ping",
       "AIOPS_FALLBACK_AFTER_MINUTES": "5", "AIOPS_JOURNAL_DIR": ""}


class GuardTest(unittest.TestCase):
    def make(self, states):
        self.posts, self.gets, self.logs = [], [], []
        it = iter(states)
        return Guard(self.logs.append, post=lambda u, p: self.posts.append(p), get=self.gets.append, alive=lambda dsn: next(it))

    def run_minutes(self, g, n):
        with mock.patch.dict(os.environ, ENV):
            for i in range(n):
                g.check(T0 + i * M)

    def test_down_notifies_after_delay_reminds_hourly_and_recovers(self):
        states = [True] + [False] * 70 + [True]
        g = self.make(states)
        self.run_minutes(g, len(states))
        self.assertEqual([p["status"] for p in self.posts], ["db_down", "db_down", "db_recovered"])
        self.assertIn("5 分鐘連不上", self.posts[0]["text"])                  # 停了第 5 分鐘才通知（不是第 1 分鐘）
        self.assertIn("65 分鐘連不上", self.posts[1]["text"])                 # 60 分鐘後再提醒
        self.assertIn("停了約 70 分鐘", self.posts[2]["text"])

    def test_short_blip_is_silent(self):
        g = self.make([True, False, False, True])
        self.run_minutes(g, 4)
        self.assertEqual(self.posts, [])
        self.assertTrue(any("恢復" in l for l in self.logs))

    def test_watchdog_only_when_db_up(self):
        g = self.make([True, False, True])
        self.run_minutes(g, 3)
        self.assertEqual(self.gets, ["https://dog/ping", "https://dog/ping"])     # 中間那分鐘沒打

    def test_broken_endpoints_only_log(self):
        g = Guard(lambda m: self.logs.append(m), post=mock.Mock(side_effect=OSError("hook down")),
                  get=mock.Mock(side_effect=OSError("dog down")), alive=lambda d: False)
        self.logs = []
        with mock.patch.dict(os.environ, {**ENV, "AIOPS_FALLBACK_AFTER_MINUTES": "0"}):
            g.check(T0)
        self.assertTrue(any("送不出去" in l for l in self.logs))

    def test_recovers_journal_when_up(self):
        g = self.make([True])
        fake = mock.Mock()
        fake.recover_journal.return_value = 3
        g.db = fake
        with mock.patch.dict(os.environ, {**ENV, "AIOPS_JOURNAL_DIR": "/tmp/j"}):
            g.check(T0)
        fake.recover_journal.assert_called_once()
        self.assertTrue(any("補寫 3 筆" in l for l in self.logs))

    def test_no_dsn_does_nothing(self):
        g = self.make([])
        with mock.patch.dict(os.environ, {**ENV, "AIOPS_PG_DSN": ""}):
            g.check(T0)
        self.assertEqual((self.posts, self.gets), ([], []))


if __name__ == "__main__":
    unittest.main()

"""detector shift（一階跳變 → 提 reset_point）的單元測試，不需要 ES。"""
import json
import unittest

import numpy as np
import pandas as pd

from detector import cmd_shift as S

TZ = "Asia/Taipei"


def rows(series, keys, values, usual=0.1, start="2026-11-01T00:00:00Z", step="30min"):
    ts = pd.date_range(start, periods=len(values), freq=step, tz="UTC")
    return pd.DataFrame({"series": series, "target": json.dumps(keys, sort_keys=True, ensure_ascii=False), "ts": ts,
                         "value": values, "median": usual})


class FindShifts(unittest.TestCase):
    K = {"service": "mes-api", "method": "GET", "route": "/lots/{id}"}

    def test_step_up_and_flat_is_a_shift(self):
        # 換版後 p95 從 0.1 跳到 0.4 停住：48 個點都往上、離散很小
        a = rows("http.latency_p95_s", self.K, [0.4 + 0.01 * ((i % 3) - 1) for i in range(48)])
        s, = S.find_shifts(a)
        self.assertEqual((s["series"], s["keys"], s["direction"], s["points"]), ("http.latency_p95_s", self.K, "up", 48))
        self.assertAlmostEqual(s["level"], 0.4, places=2)
        self.assertEqual(s["since"], pd.Timestamp("2026-11-01T00:00:00Z"))
        self.assertGreaterEqual(s["share"], 0.9)

    def test_spiky_points_are_not_a_shift(self):
        # 一直在跳（0.4 到 2.0 亂跳）：不是停在新水準，不提
        rng = np.random.default_rng(1)
        a = rows("http.latency_p95_s", self.K, list(rng.uniform(0.3, 2.0, 48)))
        self.assertEqual(S.find_shifts(a), [])

    def test_mixed_directions_are_not_a_shift(self):
        a = rows("http.requests", self.K, [200 if i % 2 else 20 for i in range(48)], usual=100)
        self.assertEqual(S.find_shifts(a), [])

    def test_too_few_points(self):
        a = rows("http.latency_p95_s", self.K, [0.4] * 10)
        self.assertEqual(S.find_shifts(a), [])
        self.assertEqual(len(S.find_shifts(a, min_points=5)), 1)

    def test_step_down_is_a_shift_too(self):
        a = rows("http.requests", self.K, [30.0] * 30, usual=100)
        s, = S.find_shifts(a)
        self.assertEqual(s["direction"], "down")


class Proposal(unittest.TestCase):
    def test_reset_point_proposed_with_exact_match(self):
        s = {"series": "http.latency_p95_s", "keys": {"service": "mes.api", "route": "/lots/{id}"},
             "since": pd.Timestamp("2026-11-01T02:00:00Z"), "direction": "up", "points": 48, "level": 0.4, "usual": 0.1, "share": 1.0, "spread": 0.05}
        d = S.proposal(s, TZ)
        self.assertEqual((d["kind"], d["status"], d["source"]), ("reset_point", "proposed", "shift"))
        self.assertEqual(d["reset_at"], "2026-11-01T10:00:00+08:00")                 # 當地時間
        self.assertEqual(d["match"], {"service": "^mes\\.api$", "route": "^/lots/\\{id\\}$"})   # 完全符合、跳脫
        self.assertEqual(d["series"], "http\\.latency_p95_s")
        self.assertIn("重新學", d["reason"])
        from detector.adjust import match_keys
        self.assertTrue(match_keys({"service": "mes.api", "route": "/lots/{id}"}, d["match"]))
        self.assertFalse(match_keys({"service": "mesXapi", "route": "/lots/{id}"}, d["match"]))


if __name__ == "__main__":
    unittest.main()

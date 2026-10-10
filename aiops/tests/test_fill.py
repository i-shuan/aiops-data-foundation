"""es._fill_zero 的單元測試：補 0 的規則，不需要 ES。"""
import unittest

import pandas as pd

from aiops_baseline.es import _fill_zero

T0 = pd.Timestamp("2026-10-01T00:00:00Z")


def ts(minutes):
    return T0 + pd.Timedelta(minutes=minutes)


class FillZeroTest(unittest.TestCase):
    def test_fills_gaps_after_first_seen(self):
        df = pd.DataFrame({"svc": ["a", "a", "b"], "ts": [ts(0), ts(10), ts(5)], "value": [3.0, 4.0, 1.0]})
        out = _fill_zero(df, ["svc"], T0, ts(15), "5m").set_index(["svc", "ts"])["value"]
        self.assertEqual(out[("a", ts(5))], 0.0)        # a 在 0 和 10 之間沒資料 → 0
        self.assertTrue(pd.isna(out[("b", ts(0))]))     # b 第一次出現是 5 分，之前不是 0，是不知道

    def test_whole_system_gap_is_unknown_not_zero(self):
        # 10 分這個時段，所有序列都沒有資料：收集系統停了
        df = pd.DataFrame({"svc": ["a", "b", "a", "b"], "ts": [ts(0), ts(0), ts(15), ts(15)], "value": [1.0, 2.0, 1.0, 2.0]})
        df = pd.concat([df, pd.DataFrame({"svc": ["a"], "ts": [ts(5)], "value": [1.0]})])
        out = _fill_zero(df, ["svc"], T0, ts(20), "5m").set_index(["svc", "ts"])["value"]
        self.assertEqual(out[("b", ts(5))], 0.0)        # 5 分 a 有資料、b 沒有 → b 是真的 0
        self.assertTrue(pd.isna(out[("a", ts(10))]))    # 10 分大家都沒資料 → 不知道
        self.assertTrue(pd.isna(out[("b", ts(10))]))


if __name__ == "__main__":
    unittest.main()

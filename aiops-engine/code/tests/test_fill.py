"""es._fill_zero 的單元測試：補 0 的規則，不需要 ES。"""
import unittest

import pandas as pd

from detector.es import _fill_zero

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

    def test_filtered_series_uses_index_alive_times(self):
        # 只數 error log 的序列：5、10 分都沒有任何服務出錯，但收集系統有在送（別的 log 有進來）→ 是真的 0，不是不知道
        df = pd.DataFrame({"svc": ["a", "a"], "ts": [ts(0), ts(15)], "value": [1.0, 2.0]})
        alive = pd.DatetimeIndex([ts(0), ts(5), ts(10), ts(15)])
        out = _fill_zero(df, ["svc"], T0, ts(20), "5m", alive).set_index(["svc", "ts"])["value"]
        self.assertEqual(out[("a", ts(5))], 0.0)
        self.assertEqual(out[("a", ts(10))], 0.0)
        # 沒給 alive（舊行為）：5、10 分會被當成收集系統停了
        old = _fill_zero(df, ["svc"], T0, ts(20), "5m").set_index(["svc", "ts"])["value"]
        self.assertTrue(pd.isna(old[("a", ts(5))]))

    def test_alive_times_excludes_real_outage(self):
        df = pd.DataFrame({"svc": ["a", "a"], "ts": [ts(0), ts(15)], "value": [1.0, 2.0]})
        alive = pd.DatetimeIndex([ts(0), ts(15)])          # 5、10 分整個 index 都沒資料：收集系統停了
        out = _fill_zero(df, ["svc"], T0, ts(20), "5m", alive).set_index(["svc", "ts"])["value"]
        self.assertTrue(pd.isna(out[("a", ts(5))]))


class AddSilentTest(unittest.TestCase):
    """detect 時有 baseline、但整段沒資料的服務補 0（cmd_baseline._add_silent）。"""
    base = pd.DataFrame({"svc": ["a", "b"]})

    def test_fills_zero_only_while_collection_alive(self):
        from detector.cmd_baseline import _add_silent
        cur = pd.DataFrame({"svc": ["a"] * 2, "ts": [ts(0), ts(5)], "value": [3.0, 3.0], "n": [3.0, 3.0]})
        out = _add_silent(cur, self.base, ["svc"], T0, ts(15), "5m", alive=lambda: pd.DatetimeIndex([ts(0), ts(5)]))
        self.assertEqual(sorted(out[out["svc"] == "b"]["ts"]), [ts(0), ts(5)])   # 10 分收集系統停了：不補

    def test_whole_window_in_outage_adds_nothing(self):
        # 收集系統整段停了（detect 看的 30 分鐘都沒資料）：不能把每個服務都補 0、報成「停掉」
        from detector.cmd_baseline import _add_silent
        empty = pd.DataFrame(columns=["svc", "ts", "value", "n"])
        out = _add_silent(empty, self.base, ["svc"], T0, ts(30), "5m", alive=lambda: pd.DatetimeIndex([]))
        self.assertTrue(out.empty)


if __name__ == "__main__":
    unittest.main()

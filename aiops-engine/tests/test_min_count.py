"""cmd_baseline.apply_min_count 的單元測試（錯誤率至少 N 個 5xx 才算異常），不需要 ES。"""
import unittest

import pandas as pd

from service.detect.baseline_job import apply_min_count


class MinCountTest(unittest.TestCase):
    def frame(self):
        # value 是錯誤率、n 是這個時間窗的請求數：1/20、2/40、3/40、6/40；最後一列本來就不是異常
        return pd.DataFrame({"svc": ["a", "b", "c", "d", "e"], "value": [0.05, 0.05, 0.075, 0.15, 0.15],
                             "n": [20, 40, 40, 40, 40], "anomaly": [True, True, True, True, False]})

    def test_fewer_than_min_count_errors_is_not_an_anomaly(self):
        s = apply_min_count(self.frame(), {"min_count": 3})
        self.assertEqual(s["anomaly"].tolist(), [False, False, True, True, False])   # 1 個、2 個不算；3 個、6 個照舊

    def test_without_min_count_nothing_changes(self):
        s = apply_min_count(self.frame(), {})
        self.assertEqual(s["anomaly"].tolist(), [True, True, True, True, False])

    def test_missing_n_or_empty_is_left_alone(self):
        f = self.frame().drop(columns="n")
        self.assertEqual(apply_min_count(f, {"min_count": 3})["anomaly"].tolist(), f["anomaly"].tolist())
        self.assertTrue(apply_min_count(self.frame().iloc[0:0], {"min_count": 3}).empty)

    def test_ratio_times_requests_is_rounded(self):
        # 3/41 ≈ 0.07317…：乘回來是 2.99999…，要算成 3 個
        f = pd.DataFrame({"value": [3 / 41], "n": [41], "anomaly": [True]})
        self.assertTrue(apply_min_count(f, {"min_count": 3})["anomaly"].item())


if __name__ == "__main__":
    unittest.main()

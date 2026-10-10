"""model.py 的單元測試，不需要 ES。執行：cd aiops && python3 -m unittest discover -s tests -t ."""
import unittest

import numpy as np
import pandas as pd

from aiops_baseline.model import DetectOptions, TrainOptions, score, train

TZ = "Asia/Taipei"


def seasonal(days: int, start: str = "2026-09-07", seed: int = 0) -> pd.DataFrame:
    """每 5 分鐘一點：白天（8～20 點）100、晚上 20、週末再打五折，加一點雜訊。start 是週一。"""
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start, periods=days * 288, freq="5min", tz=TZ)
    base = np.where((ts.hour >= 8) & (ts.hour < 20), 100.0, 20.0)
    base = np.where(ts.dayofweek >= 5, base * 0.5, base)
    value = base + rng.normal(0, base * 0.05)
    return pd.DataFrame({"svc": "mes-api", "ts": ts.tz_convert("UTC"), "value": value})


class TrainTest(unittest.TestCase):
    def test_learns_daily_and_weekly_cycle(self):
        b = train(seasonal(28), ["svc"], TrainOptions(timezone=TZ))
        self.assertEqual(len(b), 7 * 24)
        self.assertTrue((b["level"] == "dow_hour").all())
        get = lambda dow, hour: b[(b.dow == dow) & (b.hour == hour)]["median"].item()
        self.assertAlmostEqual(get(0, 10), 100, delta=3)   # 週一早上 10 點
        self.assertAlmostEqual(get(0, 3), 20, delta=1)     # 週一凌晨 3 點
        self.assertAlmostEqual(get(5, 10), 50, delta=2)    # 週六早上 10 點：週末打五折

    def test_falls_back_when_history_is_short(self):
        # 4 天：同一個星期幾只有 1 天 → 退到「每天同一個小時」
        b = train(seasonal(4), ["svc"], TrainOptions(timezone=TZ))
        self.assertTrue((b["level"] == "hour").all())
        # 1 天：hour 也不夠（要 3 天）→ 退到不分時段
        b = train(seasonal(1), ["svc"], TrainOptions(timezone=TZ))
        self.assertTrue((b["level"] == "all").all())
        # 只有 10 個點：都不夠 → 沒有 baseline
        b = train(seasonal(1).head(10), ["svc"], TrainOptions(timezone=TZ))
        self.assertTrue(b.empty)

    def test_series_are_independent(self):
        a = seasonal(28)
        other = a.assign(svc="wip-worker", value=a["value"] * 10)
        b = train(pd.concat([a, other]), ["svc"], TrainOptions(timezone=TZ))
        med = b[(b.dow == 0) & (b.hour == 10)].set_index("svc")["median"]
        self.assertAlmostEqual(med["mes-api"], 100, delta=3)
        self.assertAlmostEqual(med["wip-worker"], 1000, delta=30)


class ScoreTest(unittest.TestCase):
    def setUp(self):
        hist = seasonal(28)
        self.baseline = train(hist, ["svc"], TrainOptions(timezone=TZ))
        # 第 29 天（週一）當成「現在」
        self.today = seasonal(29, seed=1).tail(288).reset_index(drop=True)

    def test_normal_day_has_almost_no_alarms(self):
        s = score(self.today, self.baseline, ["svc"], tz=TZ)
        self.assertLessEqual(int(s["anomaly"].sum()), 2)   # 288 個點裡最多 2 個誤報

    def test_spike_and_drop_are_detected(self):
        t = self.today.copy()
        t.loc[120, "value"] = 300   # 10:00 衝到 3 倍
        t.loc[150, "value"] = 5     # 12:30 掉到幾乎 0
        s = score(t, self.baseline, ["svc"], tz=TZ)
        self.assertTrue(s.loc[120, "anomaly"])
        self.assertTrue(s.loc[150, "anomaly"])

    def test_same_value_is_normal_at_peak_but_anomaly_at_night(self):
        t = self.today.copy()
        t.loc[120, "value"] = 100   # 10:00 的 100：平常
        t.loc[24, "value"] = 100    # 02:00 的 100：平常只有 20 → 異常
        s = score(t, self.baseline, ["svc"], tz=TZ)
        self.assertFalse(s.loc[120, "anomaly"])
        self.assertTrue(s.loc[24, "anomaly"])

    def test_direction_up_ignores_drops(self):
        t = self.today.copy()
        t.loc[150, "value"] = 5
        s = score(t, self.baseline, ["svc"], direction="up", tz=TZ)
        self.assertFalse(s.loc[150, "anomaly"])

    def test_min_abs_ignores_tiny_changes(self):
        # 平常 0、偶爾 1 的錯誤數：變成 3 不該告警
        ts = pd.date_range("2026-09-07", periods=28 * 288, freq="5min", tz="UTC")
        hist = pd.DataFrame({"svc": "x", "ts": ts, "value": (np.arange(len(ts)) % 50 == 0).astype(float)})
        b = train(hist, ["svc"], TrainOptions(timezone=TZ))
        now = pd.DataFrame({"svc": ["x"], "ts": [ts[-1] + pd.Timedelta("5min")], "value": [3.0]})
        self.assertFalse(score(now, b, ["svc"], direction="up", min_abs=5, tz=TZ)["anomaly"].item())
        self.assertTrue(score(now.assign(value=20.0), b, ["svc"], direction="up", min_abs=5, tz=TZ)["anomaly"].item())

    def test_points_without_baseline_are_not_anomalies(self):
        t = self.today.assign(svc="new-service")
        s = score(t, self.baseline, ["svc"], tz=TZ)
        self.assertFalse(s["anomaly"].any())
        self.assertTrue(s["median"].isna().all())


if __name__ == "__main__":
    unittest.main()

"""model.py 的單元測試，不需要 ES。執行：cd aiops && python3 -m unittest discover -s tests -t ."""
import unittest

import numpy as np
import pandas as pd

from aiops_baseline.model import (DetectOptions, TrainOptions, mask_thin, pick_resolution, score, score_poisson,
                                  train)

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


class ResolutionTest(unittest.TestCase):
    """pick_resolution：依每個時間窗的事件數選粒度。"""

    def volume(self, per_5m: float, days: int = 7, svc: str = "api") -> pd.DataFrame:
        ts = pd.date_range("2026-09-07", periods=days * 288, freq="5min", tz="UTC")
        rng = np.random.default_rng(0)
        return pd.DataFrame({"svc": svc, "ts": ts, "n": rng.poisson(per_5m, len(ts)).astype(float)})

    def test_busy_series_stays_at_5m(self):
        r = pick_resolution(self.volume(100), ["svc"], min_events=20)
        self.assertEqual(r["resolution"].item(), "5m")

    def test_low_volume_goes_coarser(self):
        # 每 5 分鐘約 2 個請求：15m 約 6、30m 約 12、1h 約 24 → 1h 才夠 20
        self.assertEqual(pick_resolution(self.volume(2), ["svc"], min_events=20)["resolution"].item(), "1h")
        # 筆數類門檻 10：30m 就夠
        self.assertEqual(pick_resolution(self.volume(2), ["svc"], min_events=10)["resolution"].item(), "30m")

    def test_too_sparse_has_no_resolution(self):
        r = pick_resolution(self.volume(0.1), ["svc"], min_events=20)
        self.assertIsNone(r["resolution"].item())

    def test_missing_windows_count_as_zero(self):
        # 只有偶爾有請求（ES 裡沒有 bucket 的時段＝0 個請求），不能只看有資料的時段
        v = self.volume(100)
        v = v[v.index % 36 == 0]          # 每 3 小時只有一個 5 分鐘有 100 個請求；只看有資料的時段會誤選 5m
        self.assertIsNone(pick_resolution(v, ["svc"], min_events=20)["resolution"].item())

    def test_unknown_windows_are_ignored(self):
        v = self.volume(100)
        v.loc[v.index < 288 * 3, "n"] = np.nan   # 前 3 天收集系統停機：不知道，不是 0
        self.assertEqual(pick_resolution(v, ["svc"], min_events=20)["resolution"].item(), "5m")

    def test_each_series_decides_alone(self):
        v = pd.concat([self.volume(100, svc="busy"), self.volume(2, svc="quiet")])
        r = pick_resolution(v, ["svc"], min_events=20).set_index("svc")["resolution"]
        self.assertEqual((r["busy"], r["quiet"]), ("5m", "1h"))

    def test_hourly_points_fall_back_to_hour_level(self):
        # 1 小時一點、4 週：同星期幾 × 同小時只有 4 個值（< 6）→ 退到每天同一小時
        ts = pd.date_range("2026-09-07", periods=28 * 24, freq="1h", tz=TZ)
        h = pd.DataFrame({"svc": "api", "ts": ts.tz_convert("UTC"), "value": 10.0 + np.arange(len(ts)) % 3})
        b = train(h, ["svc"], TrainOptions(timezone=TZ))
        self.assertTrue((b["level"] == "hour").all())

    def test_daytime_only_api_uses_coverage(self):
        # 白天（12 小時）每 5 分鐘 2 個、晚上幾乎沒有：1 小時窗只有一半夠 20 → 中位數不夠，但 coverage 0.3 就夠
        v = self.volume(2)
        v.loc[v["ts"].dt.hour >= 12, "n"] = 0.0
        self.assertIsNone(pick_resolution(v, ["svc"], min_events=20, coverage=0.6)["resolution"].item())
        self.assertEqual(pick_resolution(v, ["svc"], min_events=20, coverage=0.3)["resolution"].item(), "1h")

    def test_mask_thin_drops_noisy_windows(self):
        df = pd.DataFrame({"svc": ["a", "a"], "ts": pd.date_range("2026-09-07", periods=2, freq="1h", tz="UTC"),
                           "value": [0.9, 0.1], "n": [3.0, 50.0]})
        out = mask_thin(df, 20)
        self.assertTrue(np.isnan(out["value"].iloc[0]))
        self.assertEqual(out["value"].iloc[1], 0.1)


class PoissonTest(unittest.TestCase):
    """很稀少的筆數：平均次數 λ，機率夠小才算異常。"""

    def setUp(self):
        rng = np.random.default_rng(3)
        ts = pd.date_range("2026-09-07", periods=28 * 24, freq="1h", tz=TZ)
        self.hist = pd.DataFrame({"eq": "EQP-01", "ts": ts.tz_convert("UTC"), "value": rng.poisson(0.3, len(ts)).astype(float)})
        self.base = train(self.hist, ["eq"], TrainOptions(timezone=TZ), method="poisson")

    def now(self, values):
        ts = pd.date_range("2026-10-05", periods=len(values), freq="1h", tz=TZ)
        return pd.DataFrame({"eq": "EQP-01", "ts": ts.tz_convert("UTC"), "value": np.array(values, dtype=float)})

    def test_learns_mean_rate(self):
        self.assertTrue((self.base["baseline_method"] == "poisson").all())
        self.assertAlmostEqual(self.base["median"].mean(), 0.3, delta=0.1)

    def test_burst_is_anomaly_but_normal_noise_is_not(self):
        rng = np.random.default_rng(9)
        normal = rng.poisson(0.3, 24).tolist()
        s = score_poisson(self.now(normal), self.base, ["eq"], direction="up", tz=TZ)
        self.assertEqual(int(s["anomaly"].sum()), 0)
        s = score_poisson(self.now([0, 0, 8, 0]), self.base, ["eq"], direction="up", tz=TZ)
        self.assertEqual(s["anomaly"].tolist(), [False, False, True, False])
        self.assertLess(s["p"].iloc[2], 1e-5)

    def test_zero_is_never_a_drop_when_rate_is_low(self):
        s = score_poisson(self.now([0] * 24), self.base, ["eq"], direction="both", tz=TZ)
        self.assertEqual(int(s["anomaly"].sum()), 0)

    def test_one_past_burst_does_not_inflate_rate(self):
        h = self.hist.copy()
        h.loc[100, "value"] = 50.0               # 歷史裡有一次事故爆量
        b = train(h, ["eq"], TrainOptions(timezone=TZ), method="poisson")
        self.assertLess(b["median"].max(), 1.0)

    def test_never_seen_slot_is_not_zero_rate(self):
        h = self.hist.assign(value=0.0)
        b = train(h, ["eq"], TrainOptions(timezone=TZ), method="poisson")
        self.assertGreater(b["median"].min(), 0)
        s = score_poisson(self.now([1]), b, ["eq"], direction="up", tz=TZ)
        self.assertFalse(s["anomaly"].item())     # 從沒發生過，出現 1 次還不算

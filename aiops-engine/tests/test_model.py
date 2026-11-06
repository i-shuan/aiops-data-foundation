"""model.py 的單元測試，不需要 ES。執行：cd aiops-engine && python3 -m unittest discover -s tests -t ."""
import unittest

import numpy as np
import pandas as pd

from detect.baseline import (TrainOptions, effective_for, group_of, mark_status, mask_thin, override_for,
                                  pick_resolution, resolutions_from, score, score_poisson, train, train_groups)


class EffectiveForTest(unittest.TestCase):
    """tune --show 用：每個參數最終生效的值是哪一筆定的（override_for 只給值，看不出來源）。"""
    ov = [{"series": "http.latency_p95_s", "match": {"service": "^mes-"}, "z": 6, "resolution": "5m"},            # series.yaml 第 1 筆
          {"id": "t1", "series": "http.latency_p95_s", "match": {"service": "^mes-report"}, "z": 7, "min_abs": 0.5},  # aiops-tuning
          {"id": "x1", "kind": "exclude_window", "series": "http.latency_p95_s", "z": 99},                        # 不是 override，不算
          {"id": "t2", "series": "log\\..*", "confirm_points": 1}]                                                # 別條序列

    def test_later_wins_and_source_is_reported(self):
        e = effective_for("http.latency_p95_s", {"service": "mes-report-api"}, self.ov)
        self.assertEqual({k: v["value"] for k, v in e.items()}, {"z": 7, "resolution": "5m", "min_abs": 0.5})
        self.assertEqual((e["z"]["source"], e["min_abs"]["source"]), ("t1", "t1"))          # 後面那筆蓋掉 z，來源跟著換
        self.assertEqual(e["resolution"]["source"], "series.yaml overrides#1")                 # 只有第一筆設了粒度，來源還是它
        self.assertEqual(e["z"]["match"], {"service": "^mes-report"})
        # 值跟 override_for 一致：--show 印的就是 detect 用的
        self.assertEqual({k: v["value"] for k, v in e.items()}, override_for("http.latency_p95_s", {"service": "mes-report-api"}, self.ov))

    def test_only_first_matches(self):
        e = effective_for("http.latency_p95_s", {"service": "mes-api"}, self.ov)
        self.assertEqual({k: (v["value"], v["source"]) for k, v in e.items()}, {"z": (6, "series.yaml overrides#1"), "resolution": ("5m", "series.yaml overrides#1")})

    def test_nothing_matches(self):
        self.assertEqual(effective_for("pod.cpu", {"service": "mes-api"}, self.ov), {})

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


class GroupTest(unittest.TestCase):
    """借同組的 baseline：組由設定的正規表示式決定，組 baseline 從成員自己的 baseline 算。"""
    GROUPS = [{"name": "mes-query", "match": {"svc": "^mes-", "route": "query"}}, {"name": "mes", "match": {"svc": "^mes-"}}]

    def test_group_of_is_ordered_fine_to_coarse(self):
        self.assertEqual(group_of({"svc": "mes-api", "route": "/lot/query"}, self.GROUPS), ["mes-query", "mes"])
        self.assertEqual(group_of({"svc": "mes-api", "route": "/lot/save"}, self.GROUPS), ["mes"])
        self.assertEqual(group_of({"svc": "eap", "route": "/query"}, self.GROUPS), [])

    def test_group_baseline_is_median_of_members(self):
        own = pd.DataFrame([{"svc": f"mes-{i}", "route": "/query", "dow": 0, "hour": 10, "level": "dow_hour",
                             "median": m, "mad": d} for i, (m, d) in enumerate([(100, 10), (120, 20), (500, 50)])])
        g = train_groups(own, ["svc", "route"], self.GROUPS, min_members=3)
        row = g[g.group == "mes-query"].iloc[0]
        self.assertEqual((row["median"], row["mad"]), (120.0, 20.0))   # 一個特別慢的成員不會把組拉偏

    def test_too_few_members_no_group(self):
        own = pd.DataFrame([{"svc": "mes-1", "route": "/query", "dow": 0, "hour": 10, "level": "all", "median": 1.0, "mad": 0.1}])
        self.assertTrue(train_groups(own, ["svc", "route"], self.GROUPS, min_members=3).empty)


class StatusTest(unittest.TestCase):
    """跳一次 warning，連續兩次 anomaly；粗粒度一個點就夠長。"""

    def frame(self, flags, step="5min", values=None):
        ts = pd.date_range("2026-10-05", periods=len(flags), freq=step, tz="UTC")
        v = values or [200.0 if f else 100.0 for f in flags]
        return pd.DataFrame({"svc": "a", "ts": ts, "value": v, "median": 100.0, "anomaly": flags})

    def test_single_jump_is_warning(self):
        s = mark_status(self.frame([False, True, False]), ["svc"], "5m")
        self.assertEqual(s["status"].tolist(), [None, "warning", None])

    def test_two_in_a_row_is_anomaly(self):
        s = mark_status(self.frame([False, True, True, True, False]), ["svc"], "5m")
        self.assertEqual(s["status"].tolist(), [None, "warning", "anomaly", "anomaly", None])

    def test_gap_resets(self):
        s = mark_status(self.frame([True, False, True]), ["svc"], "5m")
        self.assertEqual(s["status"].tolist(), ["warning", None, "warning"])

    def test_missing_window_resets(self):
        f = self.frame([True, True])
        f.loc[1, "ts"] = f.loc[0, "ts"] + pd.Timedelta("10min")      # 中間那 5 分鐘沒資料
        self.assertEqual(mark_status(f, ["svc"], "5m")["status"].tolist(), ["warning", "warning"])

    def test_direction_change_resets(self):
        s = mark_status(self.frame([True, True], values=[200.0, 10.0]), ["svc"], "5m")
        self.assertEqual(s["status"].tolist(), ["warning", "warning"])

    def test_confirm_points_one(self):
        # confirm_points 1：一次就算（錯誤率）
        s = mark_status(self.frame([False, True, False]), ["svc"], "5m", confirm_points=1)
        self.assertEqual(s["status"].tolist(), [None, "anomaly", None])

    def test_instant_z(self):
        # 偏太多（|z| ≥ instant_z）不用等第二個；沒那麼多的照舊是 warning
        f = self.frame([False, True, False, True]).assign(z=[0.0, 20.0, 0.0, 6.0])
        s = mark_status(f, ["svc"], "5m", instant_z=15)
        self.assertEqual(s["status"].tolist(), [None, "anomaly", None, "warning"])
        # 沒給 instant_z、或 z 是 NaN：照舊
        self.assertEqual(mark_status(f, ["svc"], "5m")["status"].tolist(), [None, "warning", None, "warning"])
        f2 = f.assign(z=[float("nan")] * 4)
        self.assertEqual(mark_status(f2, ["svc"], "5m", instant_z=15)["status"].tolist(), [None, "warning", None, "warning"])

    def test_hourly_point_is_long_enough(self):
        s = mark_status(self.frame([False, True], step="1h"), ["svc"], "1h")
        self.assertEqual(s["status"].tolist(), [None, "anomaly"])
        # 15 分鐘一點：還是要連續兩個（30 分鐘）
        s = mark_status(self.frame([True, True], step="15min"), ["svc"], "15m")
        self.assertEqual(s["status"].tolist(), ["warning", "anomaly"])


class TuningTest(unittest.TestCase):
    """使用者設定、tune 指令的調整；最細粒度的限制。"""

    def test_min_resolution_drops_finer(self):
        self.assertEqual(resolutions_from(["30s", "1m", "5m"], "1m"), ["1m", "5m"])
        self.assertEqual(resolutions_from(["30s", "1m"], None), ["30s", "1m"])

    def test_finer_than_data_is_not_picked(self):
        # 5 分鐘一筆的資料，不能選 30 秒（九成的窗是空的）
        ts = pd.date_range("2026-09-07", periods=7 * 288, freq="5min", tz="UTC")
        v = pd.DataFrame({"svc": "a", "ts": ts, "n": 100.0})
        self.assertEqual(pick_resolution(v, ["svc"], 20, base="5m")["resolution"].item(), "5m")

    def test_override_later_wins_and_must_match(self):
        ov = [{"series": "http\\..*", "match": {"service": "^mes-"}, "z": 6},
              {"series": "http.latency_p95_s", "match": {"service": "^mes-report"}, "resolution": "5m", "z": 8}]
        self.assertEqual(override_for("http.latency_p95_s", {"service": "mes-report"}, ov), {"z": 8, "resolution": "5m"})
        self.assertEqual(override_for("http.error_rate", {"service": "mes-api"}, ov), {"z": 6})
        self.assertEqual(override_for("pod.cpu", {"service": "mes-api"}, ov), {})

    def test_override_confirm_points_and_instant_z(self):
        ov = [{"series": "http.latency_p95_s", "match": {"service": "^pay"}, "confirm_points": 1, "instant_z": 10}]
        self.assertEqual(override_for("http.latency_p95_s", {"service": "pay-api"}, ov), {"confirm_points": 1, "instant_z": 10})
        self.assertEqual(override_for("http.latency_p95_s", {"service": "mes-api"}, ov), {})



B = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0]


class BucketFloorTest(unittest.TestCase):
    """p95 從直方圖算：σ 至少是中位數所在那一格寬度的一半（buckets 是直方圖的邊界）。"""
    def hist(self):
        ts = pd.date_range("2026-09-07", periods=28 * 288, freq="5min", tz="UTC")
        v = np.where(np.arange(len(ts)) % 97 == 0, 0.375, 0.175)      # 平常 0.175，偶爾跳一格到 0.375
        v = np.where(np.arange(len(ts)) % 89 == 0, 0.185, v)           # ES 在桶之間內插出來的值
        return ts, pd.DataFrame({"svc": "x", "ts": ts, "value": v})

    def test_floor_is_half_the_bucket_of_the_median(self):
        from detect.baseline import bucket_floor
        got = bucket_floor(np.array([0.175, 0.0375, 0.003, 12.0, np.nan]), B)
        self.assertTrue(np.allclose(got, [0.075, 0.0125, 0.0025, 1.25, 0.0025]))   # 0.1～0.25；0.025～0.05；0～0.005；超過 10 用最後一格；NaN 當 0

    def test_one_bucket_up_is_not_an_anomaly(self):
        ts, h = self.hist()
        b = train(h, ["svc"], TrainOptions(timezone=TZ))
        now = pd.DataFrame({"svc": ["x"], "ts": [ts[-1] + pd.Timedelta("5min")], "value": [0.375]})
        plain = score(now, b, ["svc"], direction="up", min_abs=0.05, tz=TZ)
        withb = score(now, b, ["svc"], direction="up", min_abs=0.05, tz=TZ, buckets=B)
        self.assertTrue(plain["anomaly"].item())                         # 原本：σ 只剩中位數的 5%，跨一格 z 約 23
        self.assertGreater(plain["z"].item(), 20)
        self.assertFalse(withb["anomaly"].item())                        # 半個桶寬 0.075：0.2 ÷ 0.075 ≈ 2.7
        self.assertAlmostEqual(withb["z"].item(), 0.2 / 0.075)

    def test_many_buckets_up_is_still_an_anomaly(self):
        ts, h = self.hist()
        b = train(h, ["svc"], TrainOptions(timezone=TZ))
        now = pd.DataFrame({"svc": ["x"], "ts": [ts[-1] + pd.Timedelta("5min")], "value": [1.75]})
        self.assertTrue(score(now, b, ["svc"], direction="up", min_abs=0.05, tz=TZ, buckets=B)["anomaly"].item())

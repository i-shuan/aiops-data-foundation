"""回饋 → 建議：特徵全部算完再查表、老師傅的原因優先、不用累積、一筆可以多個動作；回測沒效果不建議
（docs/pdca/meta/2026-11-suggest-features.md）。不連 ES。"""
import unittest

import pandas as pd

from detector import feedback as F

T0 = pd.Timestamp("2026-11-02T12:00:00Z")   # 週一 20:00 台北
TARGET = F.target_of({"service": "wip-worker", "route": "GET /wip/{lot}"})


def fp(minute, z, value=0.08, median=0.0, n=40, day=0):
    return {"at": (T0 + pd.Timedelta(days=7 * day, minutes=minute)).isoformat(), "z": z, "value": value, "median": median, "n": n}


class Features(unittest.TestCase):
    def test_each_feature_is_independent(self):
        fps = [fp(0, 9e7, day=d) for d in range(3)]
        f = F.fp_features(fps, reasons=["batch", "batch", "other"])
        self.assertEqual(f["reason"], "batch")
        self.assertIsNotNone(f["slot"])                   # 三筆都在週一 20 點
        self.assertFalse(f["z_ok"])                       # z 上億：放寬壓不掉
        self.assertTrue(f["low_volume"])                  # 0.08 × 40 ≈ 3 筆
        self.assertFalse(f["after_deploy"])
        self.assertEqual(f["max_hits"], 3)

    def test_batch_reason_finds_slot_from_one_point(self):
        self.assertIsNotNone(F.fp_features([fp(0, 9e7)], reasons=["batch"])["slot"])
        self.assertIsNone(F.fp_features([fp(0, 9e7)])["slot"])           # 沒說原因：一筆看不出時段

    def test_after_deploy(self):
        f = F.fp_features([fp(2, 20), fp(5, 18)], deploy_at=T0)
        self.assertTrue(f["after_deploy"])
        self.assertFalse(F.fp_features([fp(2, 20), fp(30, 18)], deploy_at=T0)["after_deploy"])


class Table(unittest.TestCase):
    def kinds(self, sugs):
        return [(s["kind"], s.get("effect") or {k: s[k] for k in ("z", "min_abs", "min_count", "reset_at", "start") if s.get(k) is not None}) for s in sugs]

    def test_slot_and_z_too_big_means_mute_and_do_not_learn(self):
        """這批資料的情況：週一 20 點 ＋ z 上億 → 那 1 小時不報、不學（以前只提放寬 z，擋不到）。"""
        sugs = F.suggest_for_false_positives("http.error_rate", TARGET, [fp(0, 9e7, n=200, value=0.1, day=d) for d in range(3)], 5.0, 0.05)
        self.assertEqual(self.kinds(sugs), [("recurring_override", {"mute": True, "train": "exclude"})])
        self.assertEqual(sugs[0]["schedule"]["cron"], "0 20 * * 1")
        self.assertIn("壓不掉", sugs[0]["reason"])
        self.assertEqual(sugs[0]["status"], "proposed")

    def test_slot_and_z_ok_means_relax_z_in_that_hour(self):
        sugs = F.suggest_for_false_positives("http.latency_p95_s", TARGET, [fp(0, 5.5, value=1.2, median=1.0, n=200, day=d) for d in range(3)], 5.0)
        self.assertEqual(self.kinds(sugs), [("recurring_override", {"z": 7.0})])

    def test_no_slot_and_z_ok_means_relax_z(self):
        sugs = F.suggest_for_false_positives("http.latency_p95_s", TARGET, [fp(0, 5.5, n=200, value=1.2, median=1.0), fp(60 * 30, 6.0, n=200, value=1.2, median=1.0)], 5.0)
        self.assertEqual(self.kinds(sugs), [("override", {"z": 7.0})])

    def test_no_slot_and_z_too_big_means_min_abs(self):
        sugs = F.suggest_for_false_positives("http.latency_p95_s", TARGET, [fp(0, 50, value=0.3, median=0.1, n=200), fp(60 * 30, 40, value=0.25, median=0.1, n=200)], 5.0, 0.05)
        self.assertEqual(self.kinds(sugs), [("override", {"min_abs": 0.24})])

    def test_one_feedback_is_enough(self):
        self.assertEqual(len(F.suggest_for_false_positives("http.error_rate", TARGET, [fp(0, 9e7, n=200)], 5.0)), 1)

    def test_low_volume_adds_min_count(self):
        sugs = F.suggest_for_false_positives("http.error_rate", TARGET, [fp(0, 9e7), fp(60 * 30, 8e7)], 5.0, 0.05, current_min_count=3)
        self.assertEqual(self.kinds(sugs), [("override", {"min_abs": 0.096}), ("override", {"min_count": 4})])
        self.assertIn("只有 3 筆", sugs[1]["reason"])

    def test_reason_overrides_computed_features(self):
        """老師傅說「換版」：就算算不出 slot、z 很大，也提 reset_point。"""
        sugs = F.suggest_for_false_positives("http.error_rate", TARGET, [fp(0, 9e7, n=200)], 5.0, reasons=["deploy"], deploy_at=T0 - pd.Timedelta(minutes=3))
        self.assertEqual(sugs[0]["kind"], "reset_point")
        self.assertEqual(sugs[0]["reset_at"], (T0 - pd.Timedelta(minutes=3)).isoformat())
        sugs = F.suggest_for_false_positives("http.error_rate", TARGET, [fp(0, 9e7, n=200), fp(3, 9e7, n=200)], 5.0, reasons=["loadtest"])
        self.assertEqual(sugs[0]["kind"], "exclude_window")
        self.assertEqual(sugs[0]["apply_to"], ["train", "detect"])
        sugs = F.suggest_for_false_positives("http.latency_p95_s", TARGET, [fp(0, 5.5, value=1.2, median=1.0, n=200)], 5.0, 0.05, reasons=["known_slow"])
        self.assertEqual(sugs[0].get("min_abs"), 0.24)                # z 壓得掉也不放寬 z：本來就慢是 min_abs 的事

    def test_min_count_override_is_applied_per_target(self):
        from detector.cmd_baseline import apply_min_count
        s = pd.DataFrame({"service": ["a", "b"], "route": ["r", "r"], "value": [0.1, 0.1], "n": [30, 30], "anomaly": [True, True]})
        ov = [{"kind": "override", "series": "http\\.error_rate", "match": {"service": "^a$"}, "min_count": 4, "status": "active"}]
        out = apply_min_count(s, {"name": "http.error_rate", "min_count": 3}, ["service", "route"], ov)
        self.assertEqual(out["anomaly"].tolist(), [False, True])       # a 要 4 筆（只有 3）；b 照設定檔的 3


class BacktestGate(unittest.TestCase):
    def test_no_reduction_is_not_recommended(self):
        from detector import cmd_backtest as B
        src = open(B.__file__, encoding="utf-8").read()
        self.assertIn("一筆都沒少", src)

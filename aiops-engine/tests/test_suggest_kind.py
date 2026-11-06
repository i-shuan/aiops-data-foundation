"""feedback.suggest_for_false_positives（誤報依時間、偏差選放寬方式）與 cmd_backtest.rule_backtest_result（規則回測）的單元測試。"""
import unittest

import pandas as pd

import learn.backtest as B
import learn.suggest as F
from detect.series_ops.manual_adjust import in_schedule

TZ = "Asia/Taipei"
T = F.target_of({"service": "mes-report", "route": "/close"})


def fp(at, value=150.0, median=100.0, z=6.0):
    return {"at": pd.Timestamp(at, tz=TZ).tz_convert("UTC").isoformat(), "value": value, "median": median, "z": z}


class SuggestKind(unittest.TestCase):
    def test_month_end_same_hour_is_recurring(self):
        s = F.suggest_for_false_positives("synth.requests", T, [fp("2026-08-31 20:10"), fp("2026-09-30 20:20"), fp("2026-10-31 20:05")], tz=TZ)[0]
        self.assertEqual((s["kind"], s["status"]), ("recurring_override", "proposed"))
        self.assertEqual(s["schedule"], {"cron": "0 20 * * *", "duration": "1h", "days_of_month": [-1]})
        self.assertEqual(s["effect"], {"z": 7.0})
        self.assertIn("每月最後一天 20 點", s["reason"])
        # 排出來的時段真的涵蓋月底 20 點、不涵蓋月中
        at = pd.Series([pd.Timestamp("2026-11-30 20:30", tz=TZ), pd.Timestamp("2026-11-15 20:30", tz=TZ)])
        self.assertEqual(list(in_schedule(at, s["schedule"], TZ)), [True, False])

    def test_same_weekday_hour_is_recurring(self):
        # 三個星期一 09 點（10-05、10-12、10-19 都是週一）
        s = F.suggest_for_false_positives("synth.requests", T, [fp("2026-10-05 09:10"), fp("2026-10-12 09:40"), fp("2026-10-19 09:05")], tz=TZ)[0]
        self.assertEqual(s["schedule"], {"cron": "0 9 * * 1", "duration": "1h"})
        self.assertIn("星期一 9 點", s["reason"])

    def test_high_z_small_deviation_raises_min_abs(self):
        # 平常不動的序列：z 都 ≥ 20（z 放寬到 7 也壓不掉），偏差最多 0.03
        fps = [fp("2026-10-05 10:00", 0.12, 0.1, 20), fp("2026-10-06 15:00", 0.13, 0.1, 30), fp("2026-10-08 03:00", 0.11, 0.1, 25)]
        s = F.suggest_for_false_positives("http.latency_p95_s", T, fps, current_min_abs=0.01, tz=TZ)[0]
        self.assertEqual((s["kind"], s["min_abs"]), ("override", 0.036))
        self.assertNotIn("z", s)
        self.assertIn("壓不掉", s["reason"])

    def test_otherwise_relaxes_z(self):
        fps = [fp("2026-10-05 13:00"), fp("2026-10-05 17:00"), fp("2026-10-06 09:00")]   # 時間分散、z 約 6
        s = F.suggest_for_false_positives("synth.requests", T, fps, tz=TZ)[0]
        self.assertEqual((s["kind"], s["z"]), ("override", 7.0))
        self.assertEqual(s, F.suggest_override("synth.requests", T, 3, 5.0))

    def test_missing_details_fall_back_to_z(self):
        s = F.suggest_for_false_positives("synth.requests", T, [{}, {}, {}], tz=TZ)[0]
        self.assertEqual((s["kind"], s["z"]), ("override", 7.0))


class RuleBacktest(unittest.TestCase):
    rule = {"name": "mes-api.db.recurring", "lookback": "5m", "op": ">=", "threshold": 450, "min_events": 0}
    incs = [{"id": "i1", "title": "第 1 次", "start": "2026-10-05T10:00:00+08:00", "end": "2026-10-05T10:20:00+08:00"},
            {"id": "i2", "title": "第 2 次", "start": "2026-10-06T10:00:00+08:00", "end": "2026-10-06T10:20:00+08:00"}]

    def frame(self, extra=()):
        ts = pd.date_range("2026-09-29", "2026-10-07", freq="5min", tz=TZ, inclusive="left").tz_convert("UTC")
        df = pd.DataFrame({"ts": ts, "value": 100.0, "n": 50})
        for s in ["2026-10-05 10:05", "2026-10-06 10:10", *extra]:
            df.loc[df["ts"] == pd.Timestamp(s, tz=TZ), "value"] = 600.0
        return df

    def test_catches_incidents_and_no_extra_is_recommended(self):
        r = B.rule_backtest_result(self.frame(), self.rule, self.incs, 1, TZ)
        self.assertTrue(r["recommended"], r["reason"])
        self.assertEqual((r["incidents_caught"], r["extra_fires"], r["fires"]), (2, 0, 2))

    def test_too_many_extra_fires_is_not_recommended(self):
        r = B.rule_backtest_result(self.frame(["2026-10-01 03:00", "2026-10-02 03:00"]), self.rule, self.incs, 1, TZ)
        self.assertFalse(r["recommended"])
        self.assertEqual((r["extra_fires"], r["extra_days"]), (2, 2))
        self.assertIn("多報 2 次", r["reason"])

    def test_missed_incident_is_not_recommended(self):
        r = B.rule_backtest_result(self.frame(), {**self.rule, "threshold": 700}, self.incs, 1, TZ)
        self.assertFalse(r["recommended"])
        self.assertIn("抓不到", r["reason"])

    def test_min_events_skips_thin_points(self):
        df = self.frame(["2026-10-01 03:00"])
        df.loc[df["ts"] == pd.Timestamp("2026-10-01 03:00", tz=TZ), "n"] = 3
        r = B.rule_backtest_result(df, {**self.rule, "min_events": 20}, self.incs, 1, TZ)
        self.assertEqual(r["extra_fires"], 0)

    def test_format(self):
        r = B.rule_backtest_result(self.frame(), self.rule, self.incs, 1, TZ)
        self.assertIn("規則 mes-api.db.recurring，門檻 450", B.format_backtest(r))


if __name__ == "__main__":
    unittest.main()

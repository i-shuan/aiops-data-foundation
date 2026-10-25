"""回測（detector.cmd_backtest）的結果怎麼呈現；回測本身要 ES 和 baseline，在 e2e 驗（docs/pdca/2026-10-loop-update.md）。"""
import unittest

from detector import cmd_backtest as B


class Format(unittest.TestCase):
    def test_recommended(self):
        r = {"weeks": 4, "series": ["http.latency_p95_s"], "anomalies_before": 30, "anomalies_after": 18, "incidents_total": 7,
             "incidents_caught_before": 7, "incidents_caught_after": 7, "incidents_lost": [], "missed_total": 0, "missed_caught_after": 0,
             "recommended": True, "reason": "告警 30 → 18；已確認事故 7/7 件還抓得到"}
        self.assertEqual(B.format_backtest(r), "回測 4 週（http.latency_p95_s）：建議核准（告警 30 → 18；已確認事故 7/7 件還抓得到）")

    def test_not_recommended_lists_lost(self):
        r = {"weeks": 4, "series": ["x"], "anomalies_before": 30, "anomalies_after": 2, "incidents_total": 2, "incidents_caught_before": 2,
             "incidents_caught_after": 1, "incidents_lost": ["i-1"], "missed_total": 1, "missed_caught_after": 0,
             "recommended": False, "reason": "會漏掉 1 件已確認的事故：mes-api 變慢"}
        s = B.format_backtest(r)
        self.assertIn("不建議（會漏掉 1 件已確認的事故：mes-api 變慢）", s)

    def test_no_series(self):
        self.assertEqual(B.format_backtest({"recommended": False, "reason": "沒有序列符合 x"}), "回測：沒有序列符合 x")


if __name__ == "__main__":
    unittest.main()


import pandas as pd


class Timezones(unittest.TestCase):
    """docs/pdca/2026-10-fix-critical.md 2：incident add 存的時間沒有時區，跟有時區的異常時間比不能丟 TypeError。"""
    df = pd.DataFrame({"ts": [pd.Timestamp("2026-10-05T02:05:00Z"), pd.Timestamp("2026-10-05T09:00:00Z")], "_series": ["x", "x"], "_svc": ["mes-api", "pay"]})

    def test_naive_incident_time_is_local(self):
        # 事故寫「10-05 10:00～10:20」沒時區 → 當台北時間 = 02:00～02:20 UTC → 抓到 02:05 的那筆
        self.assertTrue(B.caught(self.df, ["mes-api"], "2026-10-05 10:00", "2026-10-05 10:20", "Asia/Taipei"))
        # 當成 UTC 的話 10:00～10:20Z 沒有 mes-api 的異常
        self.assertFalse(B.caught(self.df, ["mes-api"], "2026-10-05 10:00", "2026-10-05 10:20", "UTC"))

    def test_aware_time_kept(self):
        self.assertTrue(B.caught(self.df, ["mes-api"], "2026-10-05T02:00:00+00:00", "2026-10-05T02:20:00+00:00", "Asia/Taipei"))
        self.assertEqual(B._ts("2026-10-05T02:00:00+00:00", "Asia/Taipei").tzname(), "UTC")

    def test_empty_df(self):
        self.assertFalse(B.caught(self.df.iloc[0:0], None, "2026-10-05 10:00", "2026-10-05 10:20"))

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

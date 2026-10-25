"""量化（detector.cmd_metrics）：每天幾件、有回饋的比例、誤報率、從告警到回饋多久、經驗有沒有留下來。純計算，不連 ES。"""
import unittest

import pandas as pd

from detector import cmd_metrics as M

T = pd.Timestamp("2026-10-01T00:00:00Z")


def alert(i, svc="mes-api", dept="D3120", minute=0, cat="service"):
    return {"alert_id": f"a{i}", "service": svc, "dept": dept, "category": cat, "started_at": (T + pd.Timedelta(hours=i, minutes=minute)).isoformat()}


def fb(alert_id, verdict, by="amy", minutes_after=10, i=0):
    return {"alert_id": alert_id, "verdict": verdict, "by": by,
            "@timestamp": (T + pd.Timedelta(hours=i, minutes=minutes_after)).isoformat() if alert_id else (T + pd.Timedelta(hours=1)).isoformat()}


class Compute(unittest.TestCase):
    def period(self, alerts, feedback, incidents=(), days=7):
        return M.compute({"alerts": alerts, "feedback": feedback, "incidents": list(incidents), "start": T, "end": T + pd.Timedelta(days=days)})

    def test_rates(self):
        alerts = [alert(i, dept="D3120" if i < 7 else "D1110") for i in range(10)]
        feedback = [fb("a0", "true_positive", i=0), fb("a1", "false_positive", i=1, minutes_after=30), fb("a2", "false_positive", i=2),
                    fb("a3", "wrong_root_cause", i=3, minutes_after=20), fb(None, "missed")]
        m = self.period(alerts, feedback)
        self.assertEqual(m["alerts"], 10)
        self.assertAlmostEqual(m["alerts_per_day"], 10 / 7)
        self.assertEqual(m["per_dept"], {"D3120": 7, "D1110": 3})
        self.assertEqual((m["reviewed"], m["false_positive"], m["wrong_root_cause"], m["missed"]), (4, 2, 1, 1))
        self.assertAlmostEqual(m["reviewed_rate"], 0.4)
        self.assertAlmostEqual(m["fp_rate"], 0.5)
        self.assertAlmostEqual(m["wrong_rate"], 0.25)
        self.assertEqual(m["time_to_feedback_min"], 15)   # 10、30、10、20 的中位數

    def test_same_alert_two_people_last_wins_and_first_feedback_time(self):
        alerts = [alert(0)]
        feedback = [fb("a0", "false_positive", by="amy", minutes_after=5), fb("a0", "true_positive", by="bob", minutes_after=60)]
        m = self.period(alerts, feedback)
        self.assertEqual((m["reviewed"], m["false_positive"], m["reviewers"]), (1, 0, 2))
        self.assertEqual(m["time_to_feedback_min"], 5)

    def test_heartbeat_and_data_quality_not_counted(self):
        m = self.period([alert(0), alert(1, cat="heartbeat"), alert(2, cat="data_quality")], [])
        self.assertEqual(m["alerts"], 1)
        self.assertIsNone(m["fp_rate"])
        self.assertEqual(m["reviewed_rate"], 0)

    def test_how_detected_rate(self):
        inc = [{"status": "confirmed", "how_detected": "看 Oracle 段"}, {"status": "corrected", "how_detected": ""}, {"status": "auto", "how_detected": "x"}]
        m = self.period([], [], inc)
        self.assertEqual(m["confirmed"], 2)
        self.assertAlmostEqual(m["how_detected_rate"], 0.5)

    def test_empty(self):
        m = self.period([], [])
        self.assertEqual((m["alerts"], m["reviewed"], m["missed"]), (0, 0, 0))
        self.assertIsNone(m["time_to_feedback_min"])


class Format(unittest.TestCase):
    def test_two_periods_side_by_side(self):
        before = M.compute({"alerts": [alert(i) for i in range(20)], "feedback": [fb(f"a{i}", "false_positive", i=i) for i in range(10)],
                            "incidents": [], "start": T, "end": T + pd.Timedelta(days=10)})
        after = M.compute({"alerts": [alert(i) for i in range(8)], "feedback": [fb(f"a{i}", "false_positive", i=i) for i in range(2)]
                           + [fb(f"a{i}", "true_positive", i=i) for i in range(2, 6)],
                           "incidents": [{"status": "confirmed", "how_detected": "x"}], "start": T, "end": T + pd.Timedelta(days=10)})
        text = "\n".join(M.format_metrics(after, "之後", before, "之前"))
        self.assertIn("| 指標 | 之前 | 之後 |", text)
        self.assertIn("| 件數 | 20（每天 2.0） | 8（每天 0.8） |", text)
        self.assertIn("| 誤報率（回饋裡標誤報的） | 100% | 33% |", text)
        self.assertIn("依部門：D3120 8 件", text)

    def test_single_period(self):
        text = "\n".join(M.format_metrics(M.compute({"alerts": [], "feedback": [], "incidents": [], "start": T, "end": T + pd.Timedelta(days=1)}), "x"))
        self.assertIn("| 誤報率（回饋裡標誤報的） | — |", text)


if __name__ == "__main__":
    unittest.main()

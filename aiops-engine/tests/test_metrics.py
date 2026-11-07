"""量化（feedback.cmd_metrics）：每天幾件、有回饋的比例、誤報率、從告警到回饋多久、經驗有沒有留下來。純計算，不連 ES；
載入（load_period）讀測試用的 PostgreSQL。"""
import unittest

import pandas as pd

import learn.evaluation_metrics as M
from common.infra.repos import Repos
from common.scenario import load_scenario

SC = load_scenario()

T = pd.Timestamp("2026-10-01T00:00:00Z")


class LoadPeriod(unittest.TestCase):
    """docs/pdca/phase5-learning-loop/2026-10-fix-critical.md Act「回饋跨段」：回饋跟著這段的告警（alert_id）抓，不照回饋自己的時間；
    漏報沒有 alert_id 才照時間。資料寫進測試用的 PostgreSQL（tests/conftest.py 指好的）。"""
    END = T + pd.Timedelta(days=7)

    def seed(self, alerts=(), feedback=()):
        repo = Repos(None, SC)
        repo.alerts.bulk([(a["alert_id"], a) for a in alerts])
        repo.feedback.bulk([(f"f{i}", f) for i, f in enumerate(feedback)])
        return repo

    def test_feedback_by_alert_ids_and_missed_by_time(self):
        alerts = [alert(0), alert(1), alert(2, cat="heartbeat")]
        late = fb("a1", "false_positive", i=24 * 10)          # 段結束後 3 天才標的：以前抓不到
        missed = {"verdict": "missed", "by": "amy", "@timestamp": (T + pd.Timedelta(days=2)).isoformat()}
        missed_later = {"verdict": "missed", "by": "amy", "@timestamp": (self.END + pd.Timedelta(days=1)).isoformat()}   # 段外的漏報：不算
        other = fb("zz", "true_positive", i=1)                # 不是這段的告警：不算
        p = M.load_period(self.seed(alerts, [late, missed, missed_later, other]), T, self.END)
        self.assertEqual(sorted((f.get("alert_id") or "", f["verdict"]) for f in p["feedback"]), [("", "missed"), ("a1", "false_positive")])
        m = M.compute(p)
        self.assertEqual((m["alerts"], m["reviewed"], m["false_positive"], m["missed"]), (2, 1, 1, 1))

    def test_many_alerts(self):
        p = M.load_period(self.seed([alert(i) for i in range(1001)], [fb("a1000", "true_positive", i=1000)]), T, T + pd.Timedelta(days=60))
        self.assertEqual(len(p["alerts"]), 1001)
        self.assertEqual([f["alert_id"] for f in p["feedback"]], ["a1000"])

    def test_no_alerts_still_reads_missed(self):
        missed = {"verdict": "missed", "by": "amy", "@timestamp": (T + pd.Timedelta(days=2)).isoformat()}
        p = M.load_period(self.seed([], [missed]), T, self.END)
        self.assertEqual([f["verdict"] for f in p["feedback"]], ["missed"])


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
        self.assertIsNone(m["rca_adoption_rate"])
        self.assertIsNone(m["extract_rate"])

    def test_rca_adoption_rate_counts_answered_only_last_per_alert(self):
        # 兩題正面 ÷ 有回答的：not_seen、沒填不算；一件多筆（多人、多異常）只算最後一筆
        alerts = [alert(i) for i in range(4)]
        feedback = [{**fb("a0", "true_positive", i=0), "rca_cause_useful": "yes", "rca_action_useful": "no"},
                    {**fb("a1", "true_positive", i=1), "rca_cause_useful": "yes", "rca_action_useful": "not_seen"},
                    {**fb("a2", "false_positive", i=2)},                                                         # 沒填
                    {**fb("a3", "true_positive", by="amy", minutes_after=5, i=3), "rca_cause_useful": "no", "rca_action_useful": "no"},
                    {**fb("a3", "true_positive", by="bob", minutes_after=50, i=3), "rca_cause_useful": "yes", "rca_action_useful": "yes"}]
        m = self.period(alerts, feedback)
        # a0：yes、no；a1：yes、—；a3（最後一筆 bob）：yes、yes → 正面 4、有回答 5
        self.assertEqual(m["rca_useful"], {"rca_cause_useful": {"yes": 3, "answered": 3}, "rca_action_useful": {"yes": 1, "answered": 2}})
        self.assertEqual(m["rca_answered"], 5)
        self.assertAlmostEqual(m["rca_adoption_rate"], 0.8)

    def test_extract_rate_over_feedback_with_text(self):
        alerts = [alert(i) for i in range(3)]
        feedback = [{**fb("a0", "false_positive", i=0), "feedback_text": "月底批次", "extract": {"ok": True, "suggested_action": "suppress_during_cron"}},
                    {**fb("a1", "true_positive", i=1), "feedback_text": "亂寫", "extract": {"ok": False, "reason": "回的不是 JSON"}},
                    {**fb("a2", "true_positive", i=2)}]
        m = self.period(alerts, feedback)
        self.assertEqual((m["with_text"], m["extracted"]), (2, 1))
        self.assertAlmostEqual(m["extract_rate"], 0.5)
        text = "\n".join(M.format_metrics(m, "x"))
        self.assertIn("| RCA 建議採納率（兩題正面 ÷ 有回答的） | —（0 題有回答） |", text)
        self.assertIn("| 回饋一句話抽成結構化欄位的比例 | 50%（2 件有寫） |", text)


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

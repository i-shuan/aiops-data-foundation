"""量化（feedback.cmd_metrics）：每天幾件、有回饋的比例、誤報率、從告警到回饋多久、經驗有沒有留下來。純計算，不連 ES；
載入（load_period）用假的 ES 記下送出的查詢。"""
import unittest

import pandas as pd

import learn.evaluation_metrics as M
from incident.store.models import ALERT_INDEX
from learn.store.models import FEEDBACK_INDEX
from common.infra.repos import Repos
from common.scenario import load_scenario

SC = load_scenario()

T = pd.Timestamp("2026-10-01T00:00:00Z")


class FakeES:
    """記下每個 _search 的 index 和 body；回應依 index 給固定的 hits（每筆一次就回完，不翻頁）。"""
    def __init__(self, hits_by_index: dict | None = None):
        self.calls = []
        self.hits = hits_by_index or {}

    def search(self, index, body):
        return self.req("POST", f"/{index}/_search", body)

    def req(self, method, path, body=None, **kw):
        index = path.split("/")[1]
        self.calls.append((method, index, body))
        src = self.hits.get(index, [])
        src = src(body) if callable(src) else src    # 同一個 index 依查詢給不同結果（回饋：terms 查 vs 漏報查）
        return {"hits": {"hits": [{"_id": f"{index}-{i}", "_source": s} for i, s in enumerate(src)]}}


def _filters(body):
    return body["query"]["bool"]["filter"]


def _terms_ids(body):
    """body 是 terms alert_id 的查詢就回那串 id，不是就 None。"""
    for f in _filters(body):
        if "terms" in f and "alert_id" in f["terms"]:
            return f["terms"]["alert_id"]
    return None


class LoadPeriod(unittest.TestCase):
    """docs/pdca/phase5-learning-loop/2026-10-fix-critical.md Act「回饋跨段」：回饋跟著這段的告警（alert_id）抓，不照回饋自己的時間；
    漏報沒有 alert_id 才照時間。"""
    END = T + pd.Timedelta(days=7)

    def feedback_calls(self, es):
        return [b for _, idx, b in es.calls if idx == FEEDBACK_INDEX]

    def test_feedback_by_alert_ids_and_missed_by_time(self):
        alerts = [alert(0), alert(1), alert(2, cat="heartbeat")]
        late = fb("a1", "false_positive", i=24 * 10)          # 段結束後 3 天才標的：以前抓不到
        missed = {"verdict": "missed", "by": "amy", "@timestamp": (T + pd.Timedelta(days=2)).isoformat()}

        def feedback(body):
            return [late] if _terms_ids(body) is not None else [missed]
        es = FakeES({ALERT_INDEX: alerts, FEEDBACK_INDEX: feedback})
        p = M.load_period(Repos(es, SC), T, self.END)
        calls = self.feedback_calls(es)
        self.assertEqual(len(calls), 2)
        # (a) 回饋查詢：terms alert_id，正好是這段載入的告警（含非 service 類，過濾在 compute）
        by_id = [b for b in calls if _terms_ids(b) is not None]
        self.assertEqual(len(by_id), 1)
        self.assertEqual(_terms_ids(by_id[0]), ["a0", "a1", "a2"])
        self.assertFalse(any("range" in f for f in _filters(by_id[0])))   # 不照回饋自己的時間
        # (b) 漏報：沒有 alert_id、照 @timestamp 區間
        by_time = [b for b in calls if _terms_ids(b) is None]
        self.assertEqual(len(by_time), 1)
        self.assertIn({"term": {"verdict": "missed"}}, _filters(by_time[0]))
        self.assertIn({"range": {"@timestamp": {"gte": T.isoformat(), "lt": self.END.isoformat()}}}, _filters(by_time[0]))
        self.assertEqual(by_time[0]["query"]["bool"]["must_not"], [{"exists": {"field": "alert_id"}}])
        # 兩種合在一起交給 compute：遲到的回饋算進有回饋的比例，漏報照算
        m = M.compute(p)
        self.assertEqual((m["alerts"], m["reviewed"], m["false_positive"], m["missed"]), (2, 1, 1, 1))

    def test_chunking_over_1000_ids(self):
        es = FakeES({ALERT_INDEX: [alert(i) for i in range(1001)]})
        M.load_period(Repos(es, SC), T, self.END)
        chunks = [_terms_ids(b) for b in self.feedback_calls(es) if _terms_ids(b) is not None]
        self.assertEqual(sorted(len(c) for c in chunks), [1, 1000])
        self.assertEqual(sorted(i for c in chunks for i in c), sorted(f"a{i}" for i in range(1001)))

    def test_no_alerts_still_queries_missed(self):
        es = FakeES()
        p = M.load_period(Repos(es, SC), T, self.END)
        self.assertEqual([_terms_ids(b) for b in self.feedback_calls(es)], [None])   # 沒告警就不送空的 terms
        self.assertEqual(p["feedback"], [])


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

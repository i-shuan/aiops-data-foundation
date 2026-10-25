"""Check：誤報統計、建議、事故（detector.feedback）。不需要 ES。各種測資。"""
import json
import unittest

import pandas as pd

from detector import feedback as F
from detector.model import override_for


def anomalies(rows):
    """rows：[(id, series, keys, status)]"""
    return pd.DataFrame([{"id": i, "series": s, "target": F.target_of(k), "status": st} for i, s, k, st in rows])


def feedback(rows):
    """rows：[(anomaly_id, series, keys, verdict)]，依序就是時間先後。"""
    return pd.DataFrame([{"anomaly_id": i, "series": s, "target": F.target_of(k), "verdict": v} for i, s, k, v in rows],
                        columns=["anomaly_id", "series", "target", "verdict"])


def row(s, series):
    return s["series"][s["series"]["series"] == series].iloc[0]


class Summarize(unittest.TestCase):
    lat = {"service": "mes-api", "route": "/lots"}

    def test_basic_rates(self):
        a = anomalies([(f"a{i}", "http.latency_p95_s", self.lat, "anomaly") for i in range(10)])
        f = feedback([("a0", "http.latency_p95_s", self.lat, "true_positive"), ("a1", "http.latency_p95_s", self.lat, "false_positive"),
                      ("a2", "http.latency_p95_s", self.lat, "false_positive"), ("a3", "http.latency_p95_s", self.lat, "wrong_root_cause")])
        r = row(F.summarize(a, f), "http.latency_p95_s")
        self.assertEqual((r["alerts"], r["reviewed"], r["true_positive"], r["false_positive"], r["wrong_root_cause"]), (10, 4, 1, 2, 1))
        self.assertAlmostEqual(r["fp_rate"], 0.5)
        self.assertAlmostEqual(r["reviewed_rate"], 0.4)

    def test_warnings_are_not_alerts(self):
        # warning 是「跳一次、先觀察」，沒通知任何人，不算告警
        a = anomalies([("a1", "x", self.lat, "warning"), ("a2", "x", self.lat, "warning"), ("a3", "x", self.lat, "anomaly")])
        self.assertEqual(row(F.summarize(a, feedback([])), "x")["alerts"], 1)

    def test_nothing_reviewed_has_no_fp_rate(self):
        r = row(F.summarize(anomalies([("a1", "x", self.lat, "anomaly")]), feedback([])), "x")
        self.assertTrue(pd.isna(r["fp_rate"]))
        self.assertEqual(r["reviewed_rate"], 0)

    def test_same_anomaly_judged_twice_last_wins(self):
        a = anomalies([("a1", "x", self.lat, "anomaly")])
        f = feedback([("a1", "x", self.lat, "false_positive"), ("a1", "x", self.lat, "true_positive")])
        r = row(F.summarize(a, f), "x")
        self.assertEqual((r["reviewed"], r["true_positive"], r["false_positive"]), (1, 1, 0))

    def test_missed_counted_and_listed(self):
        f = feedback([(None, "log.errors", {"service": "pay"}, "missed"), (None, "log.errors", {"service": "pay"}, "missed")])
        s = F.summarize(anomalies([("a1", "x", self.lat, "anomaly")]), f)
        self.assertEqual(row(s, "log.errors")["missed"], 2)
        self.assertEqual(len(s["missed"]), 1)   # 同一個對象只列一次

    def test_feedback_for_series_without_alerts(self):
        # 序列這段時間沒有告警（異常已過期被清掉），回饋還在：不能壞掉
        f = feedback([("gone", "old.series", self.lat, "false_positive")])
        s = F.summarize(anomalies([]), f)
        self.assertEqual(row(s, "old.series")["false_positive"], 1)

    def test_candidates_need_min_false_positive_on_same_target(self):
        other = {"service": "pay", "route": "/x"}
        a = anomalies([(f"a{i}", "http.error_rate", self.lat, "anomaly") for i in range(5)]
                      + [(f"b{i}", "http.error_rate", other, "anomaly") for i in range(5)])
        f = feedback([(f"a{i}", "http.error_rate", self.lat, "false_positive") for i in range(3)]
                     + [(f"b{i}", "http.error_rate", other, "false_positive") for i in range(2)])
        c = F.summarize(a, f, min_false_positive=3)["candidates"]
        self.assertEqual(len(c), 1)
        self.assertEqual(json.loads(c["target"][0]), self.lat)

    def test_one_person_clicking_same_anomaly_does_not_make_candidate(self):
        a = anomalies([("a1", "x", self.lat, "anomaly")])
        f = feedback([("a1", "x", self.lat, "false_positive")] * 5)
        self.assertTrue(F.summarize(a, f, min_false_positive=3)["candidates"].empty)

    def test_empty_everything(self):
        s = F.summarize(pd.DataFrame(columns=["id", "series", "target", "status"]), feedback([]))
        self.assertTrue(s["series"].empty and s["candidates"].empty)
        self.assertIn("沒有資料", F.format_summary(s, 28))

    def test_format_has_rates_and_sections(self):
        a = anomalies([(f"a{i}", "x", self.lat, "anomaly") for i in range(4)])
        f = feedback([(f"a{i}", "x", self.lat, "false_positive") for i in range(3)] + [(None, "y", self.lat, "missed")])
        text = "\n".join(F.format_summary(F.summarize(a, f), 28))
        for want in ("告警 4 筆", "誤報率 100%", "誤報累積多次", "漏報"):
            self.assertIn(want, text)


class Suggest(unittest.TestCase):
    def test_override_suggestion_is_proposed_and_relaxes_z(self):
        sug = F.suggest_override("http.latency_p95_s", F.target_of({"service": "mes-api", "route": "/lots/{id}"}), 4, 5.0)
        self.assertEqual((sug["status"], sug["source"], sug["z"]), ("proposed", "feedback", 7.0))
        self.assertIn("誤報 4 次", sug["reason"])

    def test_match_is_exact_and_escaped(self):
        # 路由裡的 { } . 不能被當成正規表示式
        keys = {"service": "mes.api", "route": "/lots/{id}"}
        sug = F.suggest_override("http.latency_p95_s", F.target_of(keys), 3)
        from detector.adjust import match_keys
        self.assertTrue(match_keys(keys, sug["match"]))
        self.assertFalse(match_keys({"service": "mesXapi", "route": "/lots/{id}"}, sug["match"]))
        self.assertFalse(match_keys({"service": "mes.api", "route": "/lots/{id}/items"}, sug["match"]))

    def test_proposed_does_not_take_effect_until_approved(self):
        from detector.adjust import active
        keys = {"service": "a"}
        sug = F.suggest_override("x.y", F.target_of(keys), 3)
        now = pd.Timestamp("2026-10-24", tz="UTC")
        self.assertEqual(active([sug], now, kinds=("override",)), [])
        approved = {**sug, "status": "active"}
        got = override_for("x.y", keys, active([approved], now, kinds=("override",)))
        self.assertEqual(got, {"z": 7.0})
        self.assertEqual(override_for("xyy", keys, [approved]), {})   # 序列名稱也跳脫了：x.y 的 . 不會對到 xyy

    def test_exclude_for_incident(self):
        inc = {"id": "inc1", "title": "DB 鎖表", "start": "2026-10-20T02:00:00Z", "end": "2026-10-20T05:00:00Z",
               "services": ["mes-api", "wip-worker"]}
        a, b = F.suggest_exclude(inc)
        self.assertEqual((a["kind"], a["status"], a["incident_id"], a["apply_to"]), ("exclude_window", "proposed", "inc1", ["train"]))
        self.assertEqual({a["match"]["service"], b["match"]["service"]}, {"^mes\\-api$", "^wip\\-worker$"})

    def test_exclude_without_services_is_global(self):
        one, = F.suggest_exclude({"id": "i", "title": "t", "start": "2026-10-20", "end": "2026-10-21"})
        self.assertNotIn("match", one)


if __name__ == "__main__":
    unittest.main()


class SuggestFromIncidents(unittest.TestCase):
    """回饋回到系統（docs/pdca/2026-10-loop-update.md）：漏報 → 降 z；同根因 3 次 → 升級成規則。"""

    def test_missed_lowers_z(self):
        s = F.suggest_missed("log.errors", F.target_of({"service": "pay"}), current_z=5.0)
        self.assertEqual((s["kind"], s["z"], s["status"]), ("override", 4.0, "proposed"))
        self.assertEqual(s["match"], {"service": "^pay$"})
        self.assertIn("漏報", s["reason"])

    def inc(self, i, svc="mes-api", cat="db", seg="mes-api › wip-worker › Oracle SELECT lots #"):
        return {"id": f"i{i}", "services": [svc], "root_cause_category": cat, "fingerprint": {"slow_segments": [seg], "error_origins": []}}

    def test_three_same_root_cause_become_rule_suggestion(self):
        out = F.suggest_rules([self.inc(1), self.inc(2), self.inc(3), self.inc(4, cat="network"), self.inc(5, svc="pay"), self.inc(6, svc="pay")])
        self.assertEqual(len(out), 1)
        s = out[0]
        self.assertEqual((s["kind"], s["service"], s["root_cause_category"], s["status"]), ("rule_suggestion", "mes-api", "db", "proposed"))
        self.assertEqual(s["incident_ids"], ["i1", "i2", "i3"])
        d = s["rule_draft"]
        self.assertEqual((d["name"], d["type"], d["op"]), ("mes-api.db.recurring", "window", ">="))
        self.assertIn("Oracle SELECT lots", d["_note"])
        self.assertIn("<填", d["threshold"])    # 門檻要人看事故填，不自己猜

    def test_below_min_recurring_nothing(self):
        self.assertEqual(F.suggest_rules([self.inc(1), self.inc(2)]), [])
        self.assertEqual(len(F.suggest_rules([self.inc(1), self.inc(2)], min_recurring=2)), 1)

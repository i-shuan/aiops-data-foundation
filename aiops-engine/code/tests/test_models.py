"""五個模型只給標準輸入（DataFrame／list）就能算，不 mock ES、不讀設定檔（check_*／fit_and_score／points_of／samples_of 住在各自的引擎裡）。"""
import unittest

import numpy as np
import pandas as pd

from detector import peer, trace
from detector.baseline import model as baseline
from detector.rules import engine as rules
from investigator.rca import analyze
from investigator.similar import similar

T0 = pd.Timestamp("2026-10-05T00:00:00Z")


class BaselineModel(unittest.TestCase):
    def test_fit_and_score_from_frames(self):
        rnd = np.random.default_rng(0)
        hist = pd.DataFrame({"service": "a", "route": "r", "ts": [T0 + pd.Timedelta(hours=h) for h in range(24 * 14)],
                             "value": rnd.normal(0.02, 0.002, 24 * 14), "n": 500})
        cur = pd.DataFrame({"service": "a", "route": "r", "ts": [T0 + pd.Timedelta(days=14, hours=h) for h in range(3)],
                            "value": [0.02, 0.5, 0.021], "n": 500})
        b, s = baseline.fit_and_score(hist, cur, ["service", "route"], direction="up", min_abs=0.05,
                                      train_opts=baseline.TrainOptions(min_weeks=1, min_days=2, min_points=20, min_slot_points=1),
                                      step="1h", confirm_points=1)
        self.assertFalse(b.empty)
        self.assertEqual(s["anomaly"].tolist(), [False, True, False])
        self.assertEqual(s["status"].tolist()[1], "anomaly")

    def test_check_names_missing_columns(self):
        with self.assertRaises(ValueError) as cm:
            baseline.check_series(pd.DataFrame({"service": ["a"], "ts": [T0]}), ["service"])
        self.assertIn("value", str(cm.exception))


class PeerModel(unittest.TestCase):
    def test_compare_with_volume_gate(self):
        df = pd.DataFrame({"deploy": "d", "pod": ["a", "b", "c", "d", "e"], "value": [0.01, 0.01, 0.02, 0.9, 0.95], "volume": [500] * 4 + [5]})
        r = peer.compare_checked(df, ["deploy"], "pod", min_volume=20, direction="up", min_abs=0.05)
        self.assertEqual(r[r["anomaly"]]["pod"].tolist(), ["d"])          # e 流量太少不比


class RulesModel(unittest.TestCase):
    def test_increase_rule_from_rows(self):
        rows = pd.DataFrame([{"@timestamp": (T0 + pd.Timedelta(minutes=m)).isoformat(), "pod": "p", "metrics.restarts": v} for m, v in ((0, 1), (5, 2))])
        rule = rules.check_rule({"name": "restart", "type": "increase", "field": "metrics.restarts", "by": ["pod"], "op": ">=", "threshold": 1})
        hits = rules.evaluate(rule, rules.points_of(rows, rule, T0, T0 + pd.Timedelta(minutes=10)), T0 + pd.Timedelta(minutes=6))
        self.assertEqual([h["key"] for h in hits], [("p",)])


class TraceModel(unittest.TestCase):
    def spans(self, db_ms):
        return trace.check_spans([
            {"trace_id": "t", "span_id": "s1", "parent": None, "service": "mes-api", "name": "GET /lots", "kind": "Server", "start_ms": 0, "dur_ms": 10 + db_ms},
            {"trace_id": "t", "span_id": "s2", "parent": "s1", "service": "mes-api", "name": "SELECT LOT", "kind": "Client", "target": "oracle",
             "start_ms": 5, "dur_ms": db_ms}])

    def test_compare_segments_finds_the_slow_one(self):
        now, ref = trace.group_traces(self.spans(300)), trace.group_traces(self.spans(20))
        diffs = {d.segment: d.delta_ms for d in analyze.compare_segments(now, ref)}
        slow = max(diffs, key=diffs.get)
        self.assertIn("oracle", slow)
        self.assertAlmostEqual(diffs[slow], 280)

    def test_check_names_missing_fields(self):
        with self.assertRaises(ValueError):
            trace.check_spans([{"trace_id": "t"}])


class SimilarModel(unittest.TestCase):
    def test_rank_only_confirmed(self):
        alert = {"alert_id": "a1", "service": "mes-api", "routes": ["GET /lots"], "series": ["http.error_rate"]}
        fp = similar.fingerprint(alert, {"segments": [{"segment": "mes-api › SELECT WIP → oracle", "delta_ms": 7}]})
        cands = similar.check_candidates([
            ("old", {"status": "confirmed", "title": "連線池", "fingerprint": {**fp, "routes": []}}),
            ("new", {"status": "proposed", "title": "沒人看過", "fingerprint": fp})])
        top = similar.rank(fp, similar.confirmed_only(cands), k=3)
        self.assertEqual([t["incident_id"] for t in top], ["old"])
        self.assertTrue(similar.format_similar(top)[0].startswith("## 分類"))
        self.assertIsNone(similar.classify(top))          # 候選沒有分類 → 不猜

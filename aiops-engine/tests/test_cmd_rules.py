"""detector rules 會把老師傅核准的「升級成規則」（aiops-tuning 的 rule_suggestion）一起跑（docs/pdca/phase5-learning-loop/2026-10-rule-threshold.md）。"""
import unittest
from unittest import mock

import detect.rules as C

DRAFT = {"name": "mes-api.db.recurring", "type": "window", "lookback": "5m", "index": "x", "filter": {}, "by": ["service"], "value": {"avg": "v"},
         "op": ">=", "threshold": 2.4, "_note": "說明", "_source": {"peaks": [1]}}


class ApprovedRules(unittest.TestCase):
    def rules(self, adjs):
        with mock.patch("detect.adjustments.load_adjustments", return_value=adjs):
            return C.approved_rules(object(), {})

    def test_only_active_with_numeric_threshold(self):
        got = self.rules([{"kind": "rule_suggestion", "status": "active", "rule_draft": DRAFT},
                          {"kind": "rule_suggestion", "status": "proposed", "rule_draft": DRAFT},                      # 還沒核准
                          {"kind": "rule_suggestion", "status": "active", "rule_draft": {**DRAFT, "threshold": None}},  # 算不出門檻
                          {"kind": "override", "status": "active", "z": 7}])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["threshold"], 2.4)
        self.assertFalse(any(k.startswith("_") for k in got[0]))     # 給人看的註記不進規則

    def test_expired_not_loaded(self):
        import pandas as pd
        now = pd.Timestamp("2026-10-28T10:00:00Z")
        with mock.patch("detect.adjustments.load_adjustments", return_value=[
                {"kind": "rule_suggestion", "status": "active", "rule_draft": {**DRAFT, "name": "past"}, "expires_at": "2026-10-28T09:59:00Z"},
                {"kind": "rule_suggestion", "status": "active", "rule_draft": {**DRAFT, "name": "future"}, "expires_at": "2026-10-28T10:01:00+00:00"},
                {"kind": "rule_suggestion", "status": "active", "rule_draft": {**DRAFT, "name": "never"}}]):
            got = C.approved_rules(object(), {}, now)
        self.assertEqual([r["name"] for r in got], ["future", "never"])

    def test_status_must_be_exactly_active(self):
        got = self.rules([{"kind": "rule_suggestion", "status": s, "rule_draft": DRAFT} for s in ("rejected", "inactive", None, "ACTIVE")])
        self.assertEqual(got, [])

    def test_draft_without_name_skipped(self):
        draft = {k: v for k, v in DRAFT.items() if k != "name"}
        self.assertEqual(self.rules([{"kind": "rule_suggestion", "status": "active", "rule_draft": draft}]), [])

    def test_tuning_unreadable_falls_back_to_yaml_only(self):
        with mock.patch("detect.adjustments.load_adjustments", side_effect=RuntimeError("es down")):
            self.assertEqual(C.approved_rules(object(), {}), [])


if __name__ == "__main__":
    unittest.main()

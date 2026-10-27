"""detector rules 會把老師傅核准的「升級成規則」（aiops-tuning 的 rule_suggestion）一起跑（docs/pdca/phase5-learning-loop/2026-10-rule-threshold.md）。"""
import unittest
from unittest import mock

from detector import cmd_rules as C

DRAFT = {"name": "mes-api.db.recurring", "type": "window", "lookback": "5m", "index": "x", "filter": {}, "by": ["service"], "value": {"avg": "v"},
         "op": ">=", "threshold": 2.4, "_note": "說明", "_source": {"peaks": [1]}}


class ApprovedRules(unittest.TestCase):
    def rules(self, adjs):
        with mock.patch("detector.tuning.load_adjustments", return_value=adjs):
            return C.approved_rules(object(), {})

    def test_only_active_with_numeric_threshold(self):
        got = self.rules([{"kind": "rule_suggestion", "status": "active", "rule_draft": DRAFT},
                          {"kind": "rule_suggestion", "status": "proposed", "rule_draft": DRAFT},                      # 還沒核准
                          {"kind": "rule_suggestion", "status": "active", "rule_draft": {**DRAFT, "threshold": None}},  # 算不出門檻
                          {"kind": "override", "status": "active", "z": 7}])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["threshold"], 2.4)
        self.assertFalse(any(k.startswith("_") for k in got[0]))     # 給人看的註記不進規則

    def test_tuning_unreadable_falls_back_to_yaml_only(self):
        with mock.patch("detector.tuning.load_adjustments", side_effect=RuntimeError("es down")):
            self.assertEqual(C.approved_rules(object(), {}), [])


if __name__ == "__main__":
    unittest.main()

"""learn/store/models.py：aiops-tuning 寫進去前過 Adjustment（validated）：kind／status 給錯、型別不對會擋；八種建議、tune、adjust add、shift 寫的都過得了；
mapping 的欄位都在模型裡。讀的那端（detector/adjust.py）還是 dict，這裡不測。"""
import unittest

import pandas as pd
from pydantic import ValidationError

from learn.store.models import TUNING_MAPPING, Adjustment, validated
import learn.param_suggester as F


class Validated(unittest.TestCase):
    def test_mapping_fields_are_model_fields(self):
        mapped = set(TUNING_MAPPING["mappings"]["properties"])
        self.assertEqual(mapped - set(Adjustment.model_fields), set())

    def test_rejects_bad_kind_status_and_types(self):
        for bad in ({"kind": "overide"}, {"status": "approved"}, {"kind": "override", "z": "high"}, {"kind": "exclude_window", "apply_to": "train"}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                validated(bad)

    def test_keeps_what_the_writers_send(self):
        """寫入端的 dict 原樣回來（extra allow：附屬欄位先放行），None 的不寫。"""
        doc = {"kind": "override", "series": "http\\.latency", "match": {"service": "^mes$"}, "z": 7.0, "min_abs": None, "reason": "誤報",
               "at": "2026-11-06T00:00:00+00:00", "status": "proposed", "source": "feedback", "incident_ids": ["i1"], "missing": ["schedule.cron"]}
        got = validated(doc)
        self.assertEqual(got, {k: v for k, v in doc.items() if v is not None})

    def test_every_suggest_output_passes(self):
        fps = [{"at": "2026-10-31T20:05:00+08:00", "value": 3.0, "median": 1.0, "z": 6.0, "n": 50}] * 3
        outs = F.suggest_for_false_positives("http.latency_p95_s", '{"service": "mes-api"}', fps, 5.0, 0.0, "Asia/Taipei")
        outs.append(F.suggest_missed("http.latency_p95_s", '{"service": "mes-api"}', 5.0))
        outs.append(F.suggest_override("http.latency_p95_s", '{"service": "mes-api"}', 3))
        outs += F.suggest_exclude({"id": "i1", "start": "2026-10-01T00:00:00+08:00", "end": "2026-10-01T01:00:00+08:00", "services": ["a", "b"], "title": "t"})
        alert = {"alert_id": "a-1", "service": "mes-api", "series": ["http.latency_p95_s"]}
        for act in ("suppress_during_cron", "widen_threshold", "add_rule"):
            outs.append(F.suggest_from_text({"target_service": "mes-api", "suggested_action": act, "identified_cause": "scheduled_batch_job", "feedback_type": "false_positive"},
                                            alert, "每月初批次"))
        rows = pd.DataFrame([{"series": "s", "alerts": 20, "reviewed": 12, "precision": 0.5}])
        outs += F.suggest_by_precision(rows, 5.0)
        self.assertGreaterEqual(len(outs), 8)
        for sug in outs:
            with self.subTest(kind=sug.get("kind")):
                self.assertEqual(validated({**sug, "at": "2026-11-06T00:00:00+00:00", "by": "feedback"})["kind"], sug["kind"])


if __name__ == "__main__":
    unittest.main()

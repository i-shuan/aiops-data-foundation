"""feedback export（feedback/cmd_feedback.py）：回饋＋異常合成一列，給 ML 團隊重訓。假的 ES。"""
import json
import os
import tempfile
import unittest

from shared.repos import Repos

import pandas as pd

import learn.feedback_service as C


class FakeES:
    def __init__(self, feedback, anomalies):
        self.feedback, self.anomalies = feedback, anomalies

    def search(self, index, body):
        return self.req("POST", f"/{index}/_search", body)

    def req(self, method, path, body=None, ok=None):
        if "aiops-feedback/_search" in path:
            return {"hits": {"hits": [{"_id": f"fb{i}", "_source": f} for i, f in enumerate(self.feedback)]}}
        if "_mget" in path:
            return {"docs": [{"_id": i, "found": i in self.anomalies, "_source": self.anomalies.get(i, {})} for i in body["ids"]]}
        raise AssertionError(path)


FB = [{"@timestamp": "2026-11-04T07:00:00Z", "verdict": "false_positive", "fp_reason": "pm_just_done", "by": "chen", "alert_id": "a-1", "anomaly_id": "x1"},
      {"@timestamp": "2026-11-04T07:01:00Z", "verdict": "true_positive", "by": "chen", "alert_id": "a-2", "anomaly_id": "x2"},
      {"@timestamp": "2026-11-04T07:02:00Z", "verdict": "false_positive", "by": "lin", "alert_id": "a-3", "anomaly_id": "gone", "series": "fdc.flow", "keys": {"equipment": "CMP-01"}}]
AN = {"x1": {"@timestamp": "2026-11-04T06:00:00Z", "series": "fdc.pressure", "keys": {"equipment": "ETCH-01", "lot": "LOT-1"}, "value": 12.8, "median": 9.5,
             "z": 7.2, "level": "ml", "status": "anomaly", "severity": "high", "source": "ml", "source_id": "R1"},
      "x2": {"@timestamp": "2026-11-04T06:30:00Z", "series": "fdc.temperature", "keys": {"equipment": "CVD-01"}, "value": 412.0, "source": "ml", "source_id": "R9"}}


class Export(unittest.TestCase):
    def test_rows_join_feedback_with_anomaly(self):
        rows = C.export_rows(Repos(FakeES(FB, AN)), 28, pd.Timestamp("2026-11-04T08:00:00Z"))
        self.assertEqual(len(rows), 3)
        r = rows[0]
        self.assertEqual((r["verdict"], r["fp_reason"], r["source"], r["source_id"], r["series"], r["keys"]["lot"]), ("false_positive", "pm_just_done", "ml", "R1", "fdc.pressure", "LOT-1"))
        self.assertEqual((r["value"], r["z"], r["level"]), (12.8, 7.2, "ml"))
        self.assertEqual(rows[2]["series"], "fdc.flow")                      # 異常已過期：用回饋自己帶的 series、keys
        self.assertIsNone(rows[2]["source_id"])

    def test_write_ndjson_and_csv(self):
        rows = C.export_rows(Repos(FakeES(FB, AN)), 28, pd.Timestamp("2026-11-04T08:00:00Z"))
        with tempfile.TemporaryDirectory() as d:
            p = C.write_export(rows, os.path.join(d, "fp.ndjson"))
            lines = [json.loads(l) for l in open(p, encoding="utf-8")]
            self.assertEqual(lines[0]["keys"], {"equipment": "ETCH-01", "lot": "LOT-1"})
            p = C.write_export(rows, os.path.join(d, "fp.csv"))
            df = pd.read_csv(p)
            self.assertIn("key.equipment", df.columns)                        # keys 攤成欄
            self.assertEqual(df.loc[0, "key.lot"], "LOT-1")
            self.assertEqual(len(df), 3)

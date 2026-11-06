"""工廠版接收器（detector/ingest.py）：ML／FDC 判好的異常 → aiops-anomalies 的格式。不連 ES（docs/pdca/meta/2026-11-factory-engine.md）。"""
import unittest
from datetime import datetime, timezone

import detect.factory.ingest as I

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)
ML = {"name": "ml", "index": "factory-ml-anomalies-*", "id_field": "run_id", "series": "fdc.{parameter}",
      "keys": {"equipment": "equipment_id", "chamber": "chamber_id", "recipe": "recipe_id", "lot": "lot_id", "parameter": "parameter"},
      "value": "value", "expected": "expected", "score": "score", "level": "ml",
      "status": {"field": "grade", "map": {"strong": "anomaly", "weak": "warning"}, "default": "anomaly"},
      "severity": {"field": "grade", "map": {"strong": "high", "weak": "warning"}, "default": "warning"},
      "message": "{parameter}={value}（score {score}）：{equipment_id} {chamber_id}"}
ROW = {"@timestamp": "2026-11-04T05:30:00Z", "run_id": "R1", "equipment_id": "ETCH-01", "chamber_id": "CH-B", "recipe_id": "REC-1",
       "lot_id": "LOT-1", "parameter": "pressure", "value": 12.8, "expected": 9.5, "score": 7.25, "grade": "strong", "is_anomaly": True}


class ToAnomaly(unittest.TestCase):
    def test_fields_map_to_anomaly_doc(self):
        _id, d = I.to_anomaly(ROW, ML, NOW)
        self.assertEqual(d["series"], "fdc.pressure")
        self.assertEqual(d["keys"], {"equipment": "ETCH-01", "chamber": "CH-B", "recipe": "REC-1", "lot": "LOT-1", "parameter": "pressure"})
        self.assertEqual((d["value"], d["median"], d["z"]), (12.8, 9.5, 7.25))
        self.assertEqual((d["level"], d["status"], d["severity"], d["direction"]), ("ml", "anomaly", "high", "up"))
        self.assertEqual((d["source"], d["source_id"]), ("ml", "R1"))
        self.assertEqual(d["@timestamp"], "2026-11-04T05:30:00+00:00")
        self.assertEqual(d["message"], "pressure=12.8（score 7.25）：ETCH-01 CH-B")

    def test_id_from_source_id_so_resend_overwrites(self):
        a, _ = I.to_anomaly(ROW, ML, NOW)
        b, _ = I.to_anomaly({**ROW, "value": 99.0, "@timestamp": "2026-11-04T05:31:00Z"}, ML, NOW)
        self.assertEqual(a, b)
        docs, skipped = I.convert([ROW, {**ROW, "value": 99.0}], ML, NOW)
        self.assertEqual((len(docs), skipped), (1, 0))
        self.assertEqual(docs[0][1]["value"], 99.0)        # 後面的蓋前面

    def test_id_without_source_id_uses_content(self):
        inp = {k: v for k, v in ML.items() if k != "id_field"}
        a, _ = I.to_anomaly(ROW, inp, NOW)
        b, _ = I.to_anomaly({**ROW, "value": 13.0}, inp, NOW)
        self.assertNotEqual(a, b)

    def test_weak_is_warning_only_attaches(self):
        _, d = I.to_anomaly({**ROW, "grade": "weak"}, ML, NOW)
        self.assertEqual((d["status"], d["severity"]), ("warning", "warning"))

    def test_unknown_map_value_falls_back_to_default(self):
        _, d = I.to_anomaly({**ROW, "grade": "???"}, ML, NOW)
        self.assertEqual((d["status"], d["severity"]), ("anomaly", "warning"))
        _, d = I.to_anomaly({k: v for k, v in ROW.items() if k != "grade"}, ML, NOW)
        self.assertEqual(d["status"], "anomaly")

    def test_fixed_severity_and_fdc_oos_is_critical(self):
        fdc = {"name": "fdc", "index": "x", "series": "fdc.{parameter}", "keys": {"equipment": "equipment_id", "parameter": "parameter"},
               "value": "value", "level": "fdc", "status": "anomaly",
               "severity": {"field": "alarm_level", "map": {"OOS": "critical", "OOC": "warning"}, "default": "warning"}}
        _, d = I.to_anomaly({"@timestamp": "2026-11-04T05:30:00Z", "equipment_id": "ETCH-01", "parameter": "pressure", "value": 14.4, "alarm_level": "OOS"}, fdc, NOW)
        self.assertEqual((d["level"], d["severity"], d["status"]), ("fdc", "critical", "anomaly"))
        self.assertIn("ETCH-01", d["message"])               # 沒給 message 範本：自動組一句

    def test_rows_without_time_or_keys_or_value_are_skipped(self):
        docs, skipped = I.convert([{**ROW, "@timestamp": None}, {k: v for k, v in ROW.items() if k not in ("equipment_id", "chamber_id", "recipe_id", "lot_id", "parameter")},
                                   {**ROW, "value": "n/a"}], ML, NOW)
        self.assertEqual(skipped, 2)                          # 沒時間、沒對象的略過；值壞掉的留下（value None），不丟
        self.assertEqual(len(docs), 1)
        self.assertIsNone(docs[0][1]["value"])

    def test_nested_source_and_naive_time(self):
        row = {"@timestamp": "2026-11-04 13:30:00", "run_id": "R2", "equipment": {"id": "CVD-01"}, "parameter": "temp", "value": 1}
        inp = {"name": "ml", "index": "x", "series": "fdc.{parameter}", "keys": {"equipment": "equipment.id", "parameter": "parameter"}, "value": "value", "level": "ml"}
        _, d = I.to_anomaly(row, inp, NOW)
        self.assertEqual(d["keys"]["equipment"], "CVD-01")
        self.assertEqual(d["@timestamp"], "2026-11-04T13:30:00+00:00")   # 沒時區當 UTC

    def test_validate(self):
        self.assertEqual(I.validate({"inputs": [ML]}), [])
        bad = I.validate({"inputs": [{"name": "x", "index": "i"}, {"name": "x", "index": "i", "series": "s", "keys": {"a": "b"}, "level": "dow_hour"}]})
        self.assertTrue(any("series" in b for b in bad) and any("重複" in b for b in bad) and any("level" in b for b in bad))
        self.assertEqual(I.validate({}), ["沒有 inputs"])

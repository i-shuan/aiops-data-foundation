"""事件整合的兩個防護（docs/pdca/phase5-learning-loop/2026-10-fix-critical.md 1、3）：LLM 失敗不擋這一輪；已確認的事故不會被先讀再寫蓋掉。"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from investigator import cmd_alert as C

AL = {"alert_id": "a-1", "service": "mes-api", "started_at": "2026-10-25T02:00:00+00:00", "updated_at": "2026-10-25T02:12:00+00:00",
      "routes": [], "series": ["x"], "severity": "warning", "dept": "D3120"}


class LlmDoesNotBlock(unittest.TestCase):
    def test_build_pack_exception_returns_none(self):
        with mock.patch.object(C.LLM, "build_pack", side_effect=ValueError("cannot convert float NaN to integer")):
            self.assertIsNone(C.llm_summary(AL, {}, [], {"llm": True}))

    def test_summarize_exception_returns_none(self):
        with mock.patch.object(C.LLM, "summarize", side_effect=RuntimeError("boom")):
            self.assertIsNone(C.llm_summary(AL, {}, [], {"llm": True}))

    def test_disabled(self):
        self.assertIsNone(C.llm_summary(AL, {}, [], {"llm": False}))

    def test_normal(self):
        r = C.llm_summary(AL, {"anomalies": [{"series": "x", "value": 3.0, "median": 1.0, "z": 9}]}, [], {"llm": True, "llm_provider": "template"})
        self.assertTrue(r["ok"])
        self.assertIn("[A1]", r["text"])


class FakeSession:
    def __init__(self, resp):
        self.resp, self.sent = resp, None

    def post(self, url, data=None, headers=None, timeout=None):
        self.sent = data
        class R:
            status_code = 200
            text = ""
            def json(self_inner):
                return self.resp
        return R()


class FakeES:
    url = "http://es"

    def __init__(self, resp):
        self.s = FakeSession(resp)
        self.ensured = []

    def ensure_index(self, name, body):
        self.ensured.append(name)


class IncidentsAreNotOverwritten(unittest.TestCase):
    def test_bulk_update_with_status_guard(self):
        es = FakeES({"errors": False, "items": [{"update": {"_id": "a-1", "result": "noop"}}, {"update": {"_id": "a-2", "result": "created"}}]})
        n = C.write_incidents(es, [("a-1", {"status": "auto", "title": "x"}), ("a-2", {"status": "auto", "title": "y"})])
        self.assertEqual(n, 1)
        lines = [json.loads(l) for l in es.s.sent.strip().split("\n")]
        self.assertEqual(lines[0], {"update": {"_index": C.INCIDENT_INDEX, "_id": "a-1"}})
        body = lines[1]
        # 判斷和寫入在 ES 裡一次做：狀態不是 auto 就 ctx.op = 'none'，一個欄位都不動；不存在就 upsert
        self.assertIn("ctx._source.status == 'auto'", body["script"]["source"])
        self.assertIn("ctx.op = 'none'", body["script"]["source"])
        self.assertEqual(body["script"]["params"]["doc"], {"status": "auto", "title": "x"})
        self.assertEqual(body["upsert"], {"status": "auto", "title": "x"})
        self.assertEqual(es.ensured, [C.INCIDENT_INDEX])

    def test_bulk_error_raises(self):
        es = FakeES({"errors": True, "items": [{"update": {"_id": "a-1", "error": {"reason": "bad script"}}}]})
        with self.assertRaises(RuntimeError):
            C.write_incidents(es, [("a-1", {"status": "auto"})])

    def test_empty(self):
        self.assertEqual(C.write_incidents(FakeES({}), []), 0)


class AnomaliesCarryAlertId(unittest.TestCase):
    """併件時把 alert_id 寫回 aiops-anomalies：儀表板「這件事故的異常」依 alert_id 撈，規則觸發的（沒有 status）也撈得到。"""

    def test_bulk_update_alert_id(self):
        es = FakeES({"errors": False, "items": [{"update": {"_id": "x1", "result": "updated"}}, {"update": {"_id": "x2", "result": "noop"}}]})
        n = C.tag_anomalies(es, "aiops-anomalies", [("x1", "a-1"), ("x2", "a-1")])
        self.assertEqual(n, 2)
        lines = [json.loads(l) for l in es.s.sent.strip().split("\n")]
        self.assertEqual(lines[0], {"update": {"_index": "aiops-anomalies", "_id": "x1"}})
        self.assertEqual(lines[1], {"doc": {"alert_id": "a-1"}})
        self.assertEqual(es.ensured, ["aiops-anomalies"])   # 舊 index 補 alert_id 欄位（mapping 是 dynamic false）

    def test_missing_anomaly_is_skipped(self):
        # 保留期過了、異常已刪：那一筆 document_missing，其他照寫，不擋這一輪
        es = FakeES({"errors": True, "items": [{"update": {"_id": "x1", "error": {"type": "document_missing_exception"}}},
                                               {"update": {"_id": "x2", "result": "updated"}}]})
        self.assertEqual(C.tag_anomalies(es, "aiops-anomalies", [("x1", "a-1"), ("x2", "a-1")]), 1)

    def test_other_error_raises(self):
        es = FakeES({"errors": True, "items": [{"update": {"_id": "x1", "error": {"type": "index_closed_exception"}}}]})
        with self.assertRaises(RuntimeError):
            C.tag_anomalies(es, "aiops-anomalies", [("x1", "a-1")])

    def test_empty(self):
        es = FakeES({})
        self.assertEqual(C.tag_anomalies(es, "aiops-anomalies", []), 0)
        self.assertEqual(es.ensured, [])


if __name__ == "__main__":
    unittest.main()


class StampIsInThePast(unittest.TestCase):
    """docs/pdca/phase3-locate/2026-10-alert-flap.md：覆蓋寫入剛好落在 Grafana 排定評估與送出查詢之間時，
    @timestamp 不能晚於查詢範圍的結束點，不然開著的件會被當成恢復。"""

    def test_open_and_heartbeat_are_written_one_minute_back(self):
        now = datetime(2026, 10, 27, 13, 55, 21, tzinfo=timezone.utc)
        self.assertEqual(C.stamp_of(now), datetime(2026, 10, 27, 13, 54, 21, tzinfo=timezone.utc))
        # Grafana 在 13:55:20 排定、13:55:25 才送查詢：寫入的 @timestamp 要 ≤ 13:55:20、而且還在 5 分鐘窗裡
        eval_at = datetime(2026, 10, 27, 13, 55, 20, tzinfo=timezone.utc)
        self.assertLessEqual(C.stamp_of(now), eval_at)
        self.assertGreater(C.stamp_of(now), eval_at - timedelta(minutes=5))

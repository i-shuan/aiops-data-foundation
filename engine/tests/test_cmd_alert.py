"""事件整合的兩個防護（docs/pdca/2026-10-fix-critical.md 1、3）：LLM 失敗不擋這一輪；已確認的事故不會被先讀再寫蓋掉。"""
import json
import unittest
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


if __name__ == "__main__":
    unittest.main()

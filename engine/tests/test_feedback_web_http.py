"""回饋網頁的 HTTP 層（feedback_web.__main__）：ES 掛了要回 503 和說明，不能斷線（docs/pdca/2026-10-fix-feedback-ux.md 7）。"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from feedback_web import __main__ as W
from feedback_web import app as A
from tests.test_feedback_web import ALERT, FakeES


class BrokenES(FakeES):
    """告警讀得到，但寫回饋時 ES 回 503。"""
    def bulk_index(self, index, docs):
        raise RuntimeError("POST /_bulk → 503: no shard available")


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        W.Handler.es = BrokenES()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), W.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, path, body, ctype):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_api_returns_503_with_message(self):
        with mock.patch.object(A, "_ready", set()):
            st, body = self.post("/api/feedback", json.dumps({"alert_id": ALERT["alert_id"], "verdict": "true_positive", "by": "amy"}).encode(), "application/json")
        self.assertEqual(st, 503)
        self.assertIn("沒有存成功", json.loads(body)["error"])
        self.assertIn("重送不會重複", json.loads(body)["error"])

    def test_form_returns_page_with_error(self):
        with mock.patch.object(A, "_ready", set()):
            st, body = self.post("/feedback", f"alert_id={ALERT['alert_id']}&verdict=true_positive&by=amy".encode(), "application/x-www-form-urlencoded")
        self.assertEqual(st, 503)
        self.assertIn("沒有存成功", body)
        self.assertIn(ALERT["title"], body)     # 表單還在，可以直接再送

    def test_bad_verdict_is_400(self):
        st, body = self.post("/api/feedback", json.dumps({"alert_id": ALERT["alert_id"], "verdict": "maybe"}).encode(), "application/json")
        self.assertEqual(st, 400)

    def test_health(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=10) as r:
            self.assertEqual(r.read(), b"ok")


if __name__ == "__main__":
    unittest.main()

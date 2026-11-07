"""回饋網頁的 HTTP 層（api.server，FastAPI）：ES 掛了要回 503 和說明，不能斷線（docs/pdca/phase5-learning-loop/2026-10-fix-feedback-ux.md 7）；
換成 FastAPI 之後路徑、狀態碼、回應的 bytes、Content-Type 要跟 http.server 版一樣（Grafana 直接打）。TestClient，不開真的 socket。"""
import json
import unittest
import urllib.parse
from unittest import mock

from fastapi.testclient import TestClient

import interface.web.routes as W
import interface.web.dto as D
import learn.feedback_service as F
from learn.store.models import FEEDBACK_TABLE
from tests.test_api import ALERT, FakeES


class BrokenES(FakeES):
    """告警讀得到，但一次寫好幾筆回饋時資料庫回錯（漏報是單筆寫，寫得進去）。"""
    def __init__(self):
        super().__init__()
        self.broken = mock.patch("learn.store.feedback_repo.FeedbackRepository.bulk", side_effect=RuntimeError("connection to server lost"))


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(W.app)

    def setUp(self):                  # 每個測試結束資料庫會清空（tests/conftest.py），所以每次都重新放告警
        es = BrokenES()
        W.set_es(es)
        es.broken.start()
        self.addCleanup(es.broken.stop)

    def post(self, path, body, ctype):
        r = self.c.post(path, content=body, headers={"Content-Type": ctype})
        return r.status_code, r.text

    def test_api_returns_503_with_message(self):
        with mock.patch.object(F, "_ready", set()):
            st, body = self.post("/api/feedback", json.dumps({"alert_id": ALERT["alert_id"], "verdict": "true_positive", "by": "amy"}).encode(), "application/json")
        self.assertEqual(st, 503)
        self.assertIn("沒有存成功", json.loads(body)["error"])
        self.assertIn("重送不會重複", json.loads(body)["error"])

    def test_form_returns_page_with_error(self):
        with mock.patch.object(F, "_ready", set()):
            st, body = self.post("/feedback", f"alert_id={ALERT['alert_id']}&verdict=true_positive&by=amy".encode(), "application/x-www-form-urlencoded")
        self.assertEqual(st, 503)
        self.assertIn("沒有存成功", body)
        self.assertIn(ALERT["title"], body)     # 表單還在，可以直接再送

    def test_bad_verdict_is_400(self):
        st, body = self.post("/api/feedback", json.dumps({"alert_id": ALERT["alert_id"], "verdict": "maybe"}).encode(), "application/json")
        self.assertEqual(st, 400)

    def test_health(self):
        r = self.c.get("/health")
        self.assertEqual((r.status_code, r.content, r.headers["content-type"]), (200, b"ok", "text/plain"))     # 純 text/plain，沒被補 charset

    def test_missed_form_is_flat_empty_json(self):
        r = self.c.get("/api/missed-form")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content), {k: "" for k in D.MISSED_FIELDS})

    def test_missed_bad_series_is_400_good_is_200(self):
        st, body = self.post("/api/missed", json.dumps({"series": "nope", "service": "mes-api", "at": "2026-10-25 10:00"}).encode(), "application/json")
        self.assertEqual(st, 400)
        self.assertIn("series 要是", json.loads(body)["error"])
        with mock.patch.object(F, "_ready", set()):     # BrokenES 只壞一次寫好幾筆的；漏報是單筆，寫得進去
            st, body = self.post("/api/missed", json.dumps({"series": "http.latency_p95_s", "service": "mes-api", "at": "2026-10-25 10:00", "by": "amy"}).encode(), "application/json")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["verdict"], "missed")
        self.assertTrue(any(d.get("verdict") == "missed" for d in W.state.es.written[FEEDBACK_TABLE].values()))

    def test_missed_db_down_is_503_with_message(self):
        with mock.patch("learn.store.feedback_repo.FeedbackRepository.put", side_effect=RuntimeError("connection refused")):
            st, body = self.post("/api/missed", json.dumps({"series": "http.latency_p95_s", "service": "mes-api", "at": "2026-10-25 10:00"}).encode(), "application/json")
        self.assertEqual(st, 503)
        self.assertIn("沒有存成功", json.loads(body)["error"])


class Wire(unittest.TestCase):
    """跟 http.server 版一樣的線上格式：JSON 是 D.to_json 的 bytes 原樣、表單的 checked 是 list、沒有的路／方法不對回 404 not found。"""
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(W.app)

    def setUp(self):
        self.es = FakeES()
        W.set_es(self.es)

    def test_json_bytes_are_to_json_verbatim(self):
        r = self.c.get(f"/api/alert?alert={ALERT['alert_id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/json")
        self.assertEqual(r.content, D.to_json(D.form_json(D.form_data(ALERT))))     # ensure_ascii=False：中文不會被轉成 \uXXXX
        self.assertIn("變慢".encode(), r.content)
        r = self.c.get("/api/alert?alert=nope")
        self.assertEqual((r.status_code, r.content), (404, D.to_json({"error": "找不到告警 nope"})))

    def test_form_checked_reaches_submit_as_list(self):
        data = [("alert_id", ALERT["alert_id"]), ("verdict", "true_positive"), ("by", "amy"), ("checked", "Oracle AWR / 慢查詢"), ("checked", "K8s events")]
        with mock.patch.object(F, "submit", return_value={"feedback": 2, "incident": "confirmed", "by": "amy"}) as sub:
            r = self.c.post("/feedback", content=urllib.parse.urlencode(data).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "text/html; charset=utf-8")
        form = sub.call_args.args[1]
        self.assertEqual(form["checked"], ["Oracle AWR / 慢查詢", "K8s events"])
        self.assertEqual((form["verdict"], form["by"]), ("true_positive", "amy"))     # 其他欄位還是單一字串
        self.assertIn("已記錄", r.text)

    def test_html_pages_content_type(self):
        for path in (f"/feedback?alert={ALERT['alert_id']}", f"/report?alert={ALERT['alert_id']}", "/missed"):
            r = self.c.get(path)
            self.assertEqual((r.status_code, r.headers["content-type"]), (200, "text/html; charset=utf-8"), path)
        r = self.c.get("/feedback?alert=nope")
        self.assertEqual((r.status_code, r.headers["content-type"], r.text), (404, "text/plain; charset=utf-8", "找不到告警 nope"))

    def test_unknown_path_or_method_is_plain_404(self):
        for method, path in (("GET", "/nope"), ("POST", "/nope"), ("GET", "/api/feedback"), ("POST", "/health"), ("GET", "/health/")):
            r = self.c.request(method, path)
            self.assertEqual((r.status_code, r.content, r.headers["content-type"]), (404, b"not found", "text/plain"), (method, path))
        self.assertEqual(self.c.get("/docs").status_code, 404)     # 不開 FastAPI 的文件頁，路徑跟原本一樣

    def test_options_preflight(self):
        self.assertEqual(self.c.options("/api/feedback").status_code, 204)
        self.assertEqual(self.c.options("/feedback").status_code, 404)

    def test_anomalies_without_token_is_401(self):
        with mock.patch.dict("os.environ", {"AIOPS_API_TOKEN": ""}):
            r = self.c.post("/api/anomalies", content=b"{}", headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(json.loads(r.content)["error"], "伺服器沒設 AIOPS_API_TOKEN，這個端點關著")
        with mock.patch.dict("os.environ", {"AIOPS_API_TOKEN": "s3cret"}):
            r = self.c.post("/api/anomalies", content=b"{}", headers={"Content-Type": "application/json", "Authorization": "Bearer nope"})
        self.assertEqual((r.status_code, json.loads(r.content)["error"]), (401, "token 不對（Authorization: Bearer <AIOPS_API_TOKEN>）"))


if __name__ == "__main__":
    unittest.main()

"""Grafana 裡的回饋表單（configs/grafana/dashboards/aiops-feedback.json）跟回饋服務對得起來（docs/pdca/2026-10-grafana-form.md）。"""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from feedback_web import __main__ as W
from feedback_web import app as A
from tests.test_feedback_web import ALERT, FakeES

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(HERE, "..", "configs", "grafana", "dashboards", "aiops-feedback.json")
SUBMIT_FIELDS = {"alert_id", "verdict", "by", "root_cause_category", "root_cause", "action", "how_detected", "checked", "improve",
                 "fp_reason", "fp_reason_text", "note"}
DISPLAY_FIELDS = {"title", "summary", "llm", "similar"}


class Dashboard(unittest.TestCase):
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.d = json.load(f)
        self.p = self.d["panels"][0]
        self.opts = self.p["options"]

    def test_element_ids_match_service(self):
        ids = {e["id"] for e in self.opts["elements"]}
        self.assertEqual(ids - SUBMIT_FIELDS - DISPLAY_FIELDS, set(), "表單多了服務不認得的欄位")
        self.assertTrue({"alert_id", "verdict", "by", "how_detected", "root_cause_category"} <= ids)
        prefill = set(A.form_json(A.form_data(ALERT)))
        self.assertTrue(DISPLAY_FIELDS | {"alert_id"} <= prefill, "預填要有表單顯示的欄位")

    def test_options_match_service_choices(self):
        el = {e["id"]: e for e in self.opts["elements"]}
        self.assertEqual({o["value"] for o in el["verdict"]["options"]}, set(A.VERDICTS))
        self.assertEqual({o["value"] for o in el["root_cause_category"]["options"]}, {v for v, _ in A.CATEGORIES})
        self.assertEqual({o["value"] for o in el["checked"]["options"]}, set(A.CHECKED))
        self.assertEqual({o["value"] for o in el["fp_reason"]["options"]}, {v for v, _ in A.FP_REASONS})

    def test_urls_have_no_variable_except_alert(self):
        # 外掛替換變數時會 URL 編碼：網址本身不能放變數（實測 ${feedback_api} 變成 http%3A%2F%2F…）
        self.assertTrue(self.opts["initial"]["url"].endswith("/api/alert?alert=${alert}"))
        self.assertTrue(self.opts["update"]["url"].endswith("/api/feedback"))
        for u in (self.opts["initial"]["url"].replace("${alert}", ""), self.opts["update"]["url"]):
            self.assertNotIn("${", u)

    def test_panel_has_a_query(self):
        # 外掛等面板的查詢完成（data.state Done）才發預填請求；沒有查詢就永遠不預填
        self.assertTrue(self.p.get("targets"))
        self.assertEqual(self.p["type"], "volkovlabs-form-panel")


class Cors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        W.Handler.es = FakeES()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), W.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, path, method="GET", origin="http://grafana.local", body=None, ctype=None):
        h = {"Origin": origin}
        if ctype:
            h["Content-Type"] = ctype
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def test_allowed_origin(self):
        with mock.patch.dict(os.environ, {"FEEDBACK_CORS_ORIGIN": "http://grafana.local/"}):
            st, h, body = self.req(f"/api/alert?alert={ALERT['alert_id']}")
            self.assertEqual(st, 200)
            self.assertEqual(h.get("Access-Control-Allow-Origin"), "http://grafana.local")
            self.assertEqual(json.loads(body)["title"], ALERT["title"])
            st, h, _ = self.req("/api/feedback", method="OPTIONS")
            self.assertEqual(st, 204)
            self.assertIn("POST", h.get("Access-Control-Allow-Methods", ""))

    def test_other_origin_not_allowed(self):
        with mock.patch.dict(os.environ, {"FEEDBACK_CORS_ORIGIN": "http://grafana.local"}):
            st, h, _ = self.req(f"/api/alert?alert={ALERT['alert_id']}", origin="http://evil.local")
            self.assertNotIn("Access-Control-Allow-Origin", h)

    def test_not_set_means_no_cors_and_html_pages_never(self):
        with mock.patch.dict(os.environ, {"FEEDBACK_CORS_ORIGIN": ""}):
            _, h, _ = self.req(f"/api/alert?alert={ALERT['alert_id']}")
            self.assertNotIn("Access-Control-Allow-Origin", h)
        with mock.patch.dict(os.environ, {"FEEDBACK_CORS_ORIGIN": "http://grafana.local"}):
            _, h, _ = self.req(f"/feedback?alert={ALERT['alert_id']}")      # 網頁本身不開
            self.assertNotIn("Access-Control-Allow-Origin", h)

    def test_unknown_alert_404_json(self):
        st, _, body = self.req("/api/alert?alert=nope")
        self.assertEqual(st, 404)
        self.assertIn("找不到告警", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()

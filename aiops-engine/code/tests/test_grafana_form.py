"""Grafana 裡的回饋表單（grafana/helmchart/files/dashboards/aiops-feedback.json）跟回饋服務對得起來（docs/pdca/phase5-learning-loop/2026-10-grafana-form.md）。"""
import json
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import aiops.interface.web.routes as W
import aiops.interface.web.dto as D
import aiops.service.feedback.feedback_service as F
from aiops.scenario import load_scenario

SC = load_scenario()
from aiops.core.entity.feedback import FEEDBACK_INDEX
from tests.test_api import ALERT, FakeES

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(HERE, "..", "..", "..", "grafana", "helmchart", "files", "dashboards", "forms", "aiops-feedback-forms.json")   # 外掛表單版（plugin.enabled 才載入）
SUBMIT_FIELDS = {"alert_id", "verdict", "by", "root_cause_category", "root_cause", "action", "how_detected", "checked", "improve",
                 "fp_reason", "fp_reason_text", "note", "feedback_text", "rca_cause_useful", "rca_action_useful"}
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
        prefill = set(D.form_json(D.form_data(ALERT)))
        self.assertTrue(DISPLAY_FIELDS | {"alert_id"} <= prefill, "預填要有表單顯示的欄位")

    def test_options_match_service_choices(self):
        el = {e["id"]: e for e in self.opts["elements"]}
        self.assertEqual({o["value"] for o in el["verdict"]["options"]}, set(D.VERDICTS))
        self.assertEqual({o["value"] for o in el["root_cause_category"]["options"]}, {v for v, _ in SC.categories})
        self.assertEqual({o["value"] for o in el["checked"]["options"]}, set(SC.checked))
        self.assertEqual({o["value"] for o in el["fp_reason"]["options"]}, {v for v, _ in SC.fp_reasons})
        # 採納率的兩題：選項跟服務一樣（yes／no／not_seen）、永遠顯示（不看 verdict）；一句話是 textarea
        for q, _ in D.RCA_QUESTIONS:
            self.assertEqual([o["value"] for o in el[q]["options"]], [v for v, _ in D.RCA_USEFUL])
            self.assertEqual(el[q]["showIf"], "")
        self.assertEqual(el["feedback_text"]["type"], "textarea")
        self.assertEqual(el["feedback_text"]["showIf"], "")

    def test_submit_code_mentions_tuning_proposal(self):
        # 送出成功後的提示會說有沒有依一句話提建議（tuning_id）、或沒抽成（extract.ok false）
        code = self.opts["update"]["code"]
        self.assertIn("tuning_id", code)
        self.assertIn("extract", code)

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


class MissedPanel(unittest.TestCase):
    """第二個面板：漏報。元素 id 要跟 /api/missed-form 回的 key 一樣（外掛拿預填的 key 對元素 id），序列選單跟 series.yaml 一樣。"""
    def setUp(self):
        with open(DASH, encoding="utf-8") as f:
            self.d = json.load(f)
        self.p = self.d["panels"][1]
        self.opts = self.p["options"]

    def test_element_ids_match_missed_form(self):
        ids = [e["id"] for e in self.opts["elements"]]
        self.assertEqual(set(ids), set(D.missed_form_json()), "元素 id 要跟 /api/missed-form 回的 key 一樣")
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([e["uid"] for e in self.opts["elements"]], ids)

    def test_series_options_match_series_yaml(self):
        el = {e["id"]: e for e in self.opts["elements"]}
        self.assertEqual([o["value"] for o in el["series"]["options"]], F.SERIES_NAMES)
        self.assertFalse(el["series"]["allowCustomValue"])
        self.assertEqual(el["at"]["type"], "datetime")

    def test_urls_and_query(self):
        self.assertTrue(self.opts["initial"]["url"].endswith("/api/missed-form"))
        self.assertTrue(self.opts["update"]["url"].endswith("/api/missed"))
        for u in (self.opts["initial"]["url"], self.opts["update"]["url"]):
            self.assertNotIn("${", u)
        self.assertTrue(self.p.get("targets"))
        self.assertEqual((self.p["type"], self.p["pluginVersion"]), (self.d["panels"][0]["type"], self.d["panels"][0]["pluginVersion"]))
        self.assertEqual(len({p["id"] for p in self.d["panels"]}), len(self.d["panels"]))     # 面板 id 不能撞


class Cors(unittest.TestCase):
    """CORS 只開在 /api/*、Origin 要在 API_CORS_ORIGIN 裡；網頁本身（/feedback）永遠不開。"""
    @classmethod
    def setUpClass(cls):
        W.set_es(FakeES())
        cls.c = TestClient(W.app)

    def req(self, path, method="GET", origin="http://grafana.local", body=None, ctype=None):
        h = {"Origin": origin}
        if ctype:
            h["Content-Type"] = ctype
        r = self.c.request(method, path, content=body, headers=h)
        return r.status_code, r.headers, r.text

    def test_allowed_origin(self):
        with mock.patch.dict(os.environ, {"API_CORS_ORIGIN": "http://grafana.local/"}):
            st, h, body = self.req(f"/api/alert?alert={ALERT['alert_id']}")
            self.assertEqual(st, 200)
            self.assertEqual(h.get("Access-Control-Allow-Origin"), "http://grafana.local")
            self.assertEqual(json.loads(body)["title"], ALERT["title"])
            st, h, _ = self.req("/api/feedback", method="OPTIONS")
            self.assertEqual(st, 204)
            self.assertIn("POST", h.get("Access-Control-Allow-Methods", ""))
            self.assertEqual(h.get("Access-Control-Allow-Headers"), "Content-Type")
            self.assertEqual(h.get("Vary"), "Origin")
            st, h, _ = self.req("/feedback", method="OPTIONS")                       # 網頁沒有 preflight
            self.assertEqual(st, 404)

    def test_other_origin_not_allowed(self):
        with mock.patch.dict(os.environ, {"API_CORS_ORIGIN": "http://grafana.local"}):
            st, h, _ = self.req(f"/api/alert?alert={ALERT['alert_id']}", origin="http://evil.local")
            self.assertNotIn("Access-Control-Allow-Origin", h)

    def test_not_set_means_no_cors_and_html_pages_never(self):
        with mock.patch.dict(os.environ, {"API_CORS_ORIGIN": ""}):
            _, h, _ = self.req(f"/api/alert?alert={ALERT['alert_id']}")
            self.assertNotIn("Access-Control-Allow-Origin", h)
        with mock.patch.dict(os.environ, {"API_CORS_ORIGIN": "http://grafana.local"}):
            _, h, _ = self.req(f"/feedback?alert={ALERT['alert_id']}")      # 網頁本身不開
            self.assertNotIn("Access-Control-Allow-Origin", h)
            self.assertNotIn("Vary", h)

    def test_unknown_alert_404_json(self):
        st, _, body = self.req("/api/alert?alert=nope")
        self.assertEqual(st, 404)
        self.assertIn("找不到告警", json.loads(body)["error"])

    def test_api_feedback_with_text_and_ratings(self):
        # Grafana 表單送出的 JSON 多了一句話和兩題：回應帶抽出來的四欄和建議的 id；沒有模型時走 template
        form = {"alert_id": ALERT["alert_id"], "verdict": "false_positive", "by": "amy", "fp_reason": "batch",
                "feedback_text": "這是每月初的批次匯出報表，屬於預期內負載。", "rca_cause_useful": "yes", "rca_action_useful": "not_seen"}
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "template"}), mock.patch.object(F, "_ready", set()):
            st, _, body = self.req("/api/feedback", method="POST", body=json.dumps(form).encode(), ctype="application/json")
        self.assertEqual(st, 200, body)
        r = json.loads(body)
        self.assertEqual(r["extract"]["suggested_action"], "suppress_during_cron")
        self.assertTrue(r["extract"]["ok"])
        self.assertTrue(r["tuning_id"])
        es = W.state.es
        fb = next(iter(es.written[FEEDBACK_INDEX].values()))
        self.assertEqual((fb["rca_cause_useful"], fb["rca_action_useful"], fb["tuning_id"]), ("yes", "not_seen", r["tuning_id"]))
        self.assertEqual(es.written["aiops-tuning"][r["tuning_id"]]["status"], "proposed")


if __name__ == "__main__":
    unittest.main()

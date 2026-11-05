"""api 的三個新端點（docs/pdca/meta/2026-11-api-beat-worker.md）：POST /api/anomalies（token、走 ingest.yaml 的對照、寫 ⑤、丟 alert）、
POST /api/rca（標 rca_requested、結束的件不重跑、丟 alert）、GET /api/alerts；以及 alert 看到 rca_requested 會強制重跑、把標記拿掉。
假的 ES、apply_async 用 mock 接住。"""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

import tasks
from api import __main__ as W
from api import app as A
from investigator import cmd_alert as C
from tests.test_api import ALERT, FakeES

TOKEN = {"AIOPS_API_TOKEN": "s3cret", "AIOPS_PLUGIN": "factory", tasks.BROKER_ENV: "redis://fake:6379/0"}


class ES(FakeES):
    def __init__(self):
        super().__init__()
        self.alerts["a-done"] = {**ALERT, "alert_id": "a-done", "status": "resolved"}
        self.mappings = []

    def req(self, method, path, body=None, ok=(200, 201)):
        if path.endswith("/_mapping"):
            self.mappings.append(body)
            return {}
        return super().req(method, path, body, ok)

    def search(self, index, body):
        q = body["query"]
        rows = [a for a in self.alerts.values() if "match_all" in q or a["status"] == q["term"]["status"]]
        return {"hits": {"hits": [{"_source": {k: v for k, v in a.items() if k in body["_source"]}} for a in rows[: body["size"]]]}}


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), W.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        W.Handler.es = self.es = ES()

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    # ── POST /api/anomalies ──
    ROWS = [{"@timestamp": "2026-11-05T01:00:00Z", "is_anomaly": True, "equipment_id": "ETCH-01", "chamber_id": "CH-B", "recipe_id": "R1",
             "recipe_step": "3", "lot_id": "L1", "parameter": "pressure", "value": 1.9, "expected": 1.2, "score": 4.2, "run_id": "r-1"},
            {"is_anomaly": True, "equipment_id": "ETCH-01"}]       # 沒有時間：略過

    def test_anomalies_need_token(self):
        with mock.patch.dict(os.environ, {**TOKEN, "AIOPS_API_TOKEN": ""}):
            code, r = self.call("POST", "/api/anomalies", {"input": "ml", "rows": self.ROWS})
        self.assertEqual(code, 401)
        self.assertIn("AIOPS_API_TOKEN", r["error"])
        with mock.patch.dict(os.environ, TOKEN):
            code, _ = self.call("POST", "/api/anomalies", {"input": "ml", "rows": self.ROWS}, {"Authorization": "Bearer wrong"})
            self.assertEqual(code, 401)
            code, _ = self.call("POST", "/api/anomalies", {"input": "ml", "rows": self.ROWS})
            self.assertEqual(code, 401)

    def test_anomalies_written_via_ingest_mapping_and_alert_queued(self):
        written = []
        with mock.patch.dict(os.environ, TOKEN), mock.patch("detector.cmd_ingest.write_anomalies", side_effect=lambda es, docs: written.extend(docs)), \
             mock.patch.object(tasks.run_job, "apply_async") as aa:
            code, r = self.call("POST", "/api/anomalies", {"input": "ml", "rows": self.ROWS}, {"Authorization": "Bearer s3cret"})
        self.assertEqual(code, 200, r)
        self.assertEqual((r["input"], r["written"], r["skipped"], r["alert_queued"]), ("ml", 1, 1, True))
        _id, doc = written[0]
        self.assertEqual(doc["keys"]["equipment"], "ETCH-01")          # 欄位對照照 configs/factory/ingest.yaml
        self.assertEqual((doc["level"], doc["source"], doc["source_id"]), ("ml", "ml", "r-1"))
        self.assertEqual(aa.call_args.kwargs["queue"], "alert")        # 推完立刻丟一個 alert，幾秒內開件
        self.assertTrue(self.es.mappings)                               # ingest 的欄位（source、source_id）補進 mapping

    def test_anomalies_bad_requests(self):
        with mock.patch.dict(os.environ, TOKEN), mock.patch.object(tasks.run_job, "apply_async"):
            h = {"Authorization": "Bearer s3cret"}
            self.assertEqual(self.call("POST", "/api/anomalies", {"rows": []}, h)[0], 400)
            self.assertEqual(self.call("POST", "/api/anomalies", {"input": "ml", "rows": "x"}, h)[0], 400)
            self.assertEqual(self.call("POST", "/api/anomalies", {"input": "ml", "rows": [1]}, h)[0], 400)
            code, r = self.call("POST", "/api/anomalies", {"input": "nope", "rows": []}, h)
            self.assertEqual(code, 404)
            self.assertIn("ml", r["error"])
        with mock.patch.dict(os.environ, {**TOKEN, "AIOPS_PLUGIN": "observability"}), mock.patch.object(tasks.run_job, "apply_async"):
            code, r = self.call("POST", "/api/anomalies", {"input": "ml", "rows": []}, {"Authorization": "Bearer s3cret"})
            self.assertEqual(code, 404)                                  # IT 自己判，排程表沒有 ingest
            self.assertIn("沒有 ingest", r["error"])

    def test_anomalies_without_broker_still_write(self):
        with mock.patch.dict(os.environ, {**TOKEN, tasks.BROKER_ENV: ""}), mock.patch("detector.cmd_ingest.write_anomalies"), \
             mock.patch.object(tasks.run_job, "apply_async") as aa:
            code, r = self.call("POST", "/api/anomalies", {"input": "ml", "rows": self.ROWS}, {"Authorization": "Bearer s3cret"})
        self.assertEqual((code, r["written"], r["alert_queued"]), (200, 1, False))   # 一隻 pod 的模式：寫進 ES，下一分鐘排程接手
        aa.assert_not_called()

    # ── POST /api/rca ──
    def test_rca_marks_open_alert_and_queues(self):
        with mock.patch.dict(os.environ, TOKEN), mock.patch.object(tasks.run_job, "apply_async") as aa:
            code, r = self.call("POST", "/api/rca", {"alert_id": ALERT["alert_id"]})
        self.assertEqual(code, 200, r)
        self.assertTrue(r["queued"])
        index, _id, body = self.es.updates[-1]
        self.assertEqual((index, _id), (A.ALERT_INDEX, ALERT["alert_id"]))
        self.assertTrue(body["doc"]["rca_requested"])
        self.assertEqual(aa.call_args.kwargs["queue"], "alert")

    def test_rca_refuses_missing_or_resolved(self):
        with mock.patch.dict(os.environ, TOKEN), mock.patch.object(tasks.run_job, "apply_async") as aa:
            self.assertEqual(self.call("POST", "/api/rca", {"alert_id": "nope"})[0], 404)
            code, r = self.call("POST", "/api/rca", {"alert_id": "a-done"})
        self.assertEqual(code, 409)
        self.assertIn("結束", r["error"])
        self.assertEqual(self.es.updates, [])
        aa.assert_not_called()

    def test_rca_cors_for_grafana_button(self):
        """按鈕在 Grafana 的網頁裡，瀏覽器會先 OPTIONS 再 POST：跟回饋表單同一套 CORS。"""
        with mock.patch.dict(os.environ, {**TOKEN, "API_CORS_ORIGIN": "https://grafana.example.internal"}):
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/rca", method="OPTIONS", headers={"Origin": "https://grafana.example.internal"})
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 204)
                self.assertEqual(r.headers["Access-Control-Allow-Origin"], "https://grafana.example.internal")

    # ── GET /api/alerts ──
    def test_list_alerts(self):
        code, r = self.call("GET", "/api/alerts")
        self.assertEqual(code, 200)
        self.assertEqual([a["alert_id"] for a in r], [ALERT["alert_id"]])
        self.assertNotIn("report", r[0])
        code, r = self.call("GET", "/api/alerts?status=all&size=10")
        self.assertEqual(len(r), 2)
        self.assertEqual(self.call("GET", "/api/alerts?status=weird")[0], 400)


class AlertHonoursRcaRequest(unittest.TestCase):
    """investigator alert 看到 rca_requested：強制重跑（不管有沒有 trace、重試次數用完也跑）、標記拿掉不寫回去；結束的件不跑。"""
    NOW = datetime(2026, 10, 25, 2, 30, tzinfo=timezone.utc)
    BASE = {**ALERT, "category": "service", "report": "舊報告", "findings": {"requests": {"requests": 0}},
            "report_at": "2026-10-25T02:00:00+00:00", "rca_attempts": 5}      # 沒 trace、自動重試已用完：平常不會再跑

    def test_requested_reruns_and_clears_flag(self):
        al = {**self.BASE, "rca_requested": True, "rca_requested_at": "2026-10-25T02:29:00+00:00"}
        self.assertEqual(C.wants_rerun(al, self.NOW, {"rca_retry": False}), (True, True))
        self.assertNotIn("rca_requested", al)
        self.assertNotIn("rca_requested_at", al)

    def test_not_requested_follows_auto_rule(self):
        self.assertEqual(C.wants_rerun(dict(self.BASE), self.NOW, {"rca_retry": False}), (False, False))
        fresh = {**self.BASE, "rca_attempts": 1}                                # 自動重試：沒 trace、隔 2 分鐘以上、次數沒用完
        self.assertEqual(C.wants_rerun(fresh, self.NOW, {}), (True, False))

    def test_resolved_or_rca_off_never_reruns(self):
        al = {**self.BASE, "status": "resolved", "rca_requested": True}
        self.assertEqual(C.wants_rerun(al, self.NOW, {}), (False, False))
        self.assertNotIn("rca_requested", al)                                   # 標記還是拿掉，不留在文件裡
        self.assertEqual(C.wants_rerun({**self.BASE, "rca_requested": True}, self.NOW, {"rca": False}), (False, False))

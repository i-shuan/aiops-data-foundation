"""回饋入口（feedback_web.app）：表單預填、送出後寫 aiops-feedback（每筆異常一筆）和 aiops-incidents（狀態、老師傅的經驗）。假的 ES。"""
import unittest
from datetime import datetime, timezone

from feedback_web import app as A

NOW = datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)
ALERT = {"alert_id": "a-20261025-0200-mes-api", "service": "mes-api", "dept": "D3120", "group": "it", "severity": "critical", "status": "open",
         "title": "mes-api 變慢", "summary": "從 02:00 開始", "started_at": "2026-10-25T02:00:00+00:00", "updated_at": "2026-10-25T02:12:00+00:00",
         "anomaly_ids": ["x1", "x2", "gone"], "series": ["http.latency_p95_s"], "llm_summary": "Oracle 那段多花 1.4 秒 [S1]。",
         "similar": [{"start": "2026-09-12T02:00:00+00:00", "root_cause_category": "db", "root_cause": "統計資訊過期", "action": "重跑 analyze",
                      "how_detected": "看 Oracle 段", "why": ["同服務（mes-api）"]}], "report": "# 報告"}


class FakeES:
    def __init__(self):
        self.alerts = {ALERT["alert_id"]: ALERT}
        self.anoms = {"x1": {"@timestamp": "2026-10-25T02:00:00+00:00", "series": "http.latency_p95_s", "keys": {"service": "mes-api"}},
                      "x2": {"@timestamp": "2026-10-25T02:05:00+00:00", "series": "http.error_rate", "keys": {"service": "mes-api"}}}
        self.written = {}      # index → {_id: doc}
        self.updates = []      # (index, id, body)

    def req(self, method, path, body=None, ok=(200, 201)):
        if path.startswith(f"/{A.ALERT_INDEX}/_doc/"):
            _id = path.rsplit("/", 1)[1]
            return {"found": _id in self.alerts, "_source": self.alerts.get(_id)}
        if "_mget" in path:
            return {"docs": [{"_id": i, "found": i in self.anoms, "_source": self.anoms.get(i)} for i in body["ids"]]}
        if "_update/" in path:
            self.updates.append((path.split("/")[1], path.split("/")[3].split("?")[0], body))
            return {}
        if path.endswith("_refresh"):
            return {}
        raise AssertionError(path)

    def ensure_index(self, name, body):
        self.ensured = getattr(self, "ensured", []) + [name]

    def bulk_index(self, index, docs):
        self.written.setdefault(index, {}).update(dict(docs))


class FormData(unittest.TestCase):
    def test_prefill(self):
        d = A.form_data(ALERT)
        self.assertEqual(d["alert_id"], ALERT["alert_id"])
        self.assertEqual(len(d["similar"]), 1)
        html = A.render_form(d)
        for s in ("mes-api 變慢", "統計資訊過期", "怎麼看出來的", 'name="root_cause_category"', 'value="db"', "AI 摘要", "/report?alert="):
            self.assertIn(s, html)

    def test_report_page(self):
        self.assertIn("# 報告", A.render_report(ALERT))


class Submit(unittest.TestCase):
    def setUp(self):
        self.es = FakeES()

    def test_true_positive_writes_feedback_per_anomaly_and_confirms_incident(self):
        r = A.submit(self.es, {"alert_id": ALERT["alert_id"], "verdict": "true_positive", "by": "老王", "root_cause_category": "db",
                               "root_cause": "統計資訊過期", "action": "重跑 analyze", "how_detected": "只有 lots 慢",
                               "checked": ["Oracle AWR / 慢查詢", ""], "improve": "加一條 Oracle 的規則"}, NOW)
        self.assertEqual(r, {"feedback": 2, "incident": "confirmed", "by": "老王"})
        fb = list(self.es.written[A.FEEDBACK_INDEX].values())
        self.assertEqual(sorted(f["anomaly_id"] for f in fb), ["x1", "x2"])     # 不存在的 gone 不寫
        self.assertTrue(all(f["alert_id"] == ALERT["alert_id"] and f["verdict"] == "true_positive" for f in fb))
        self.assertEqual(fb[0]["series"], "http.latency_p95_s")
        (index, _id, body), = self.es.updates
        self.assertEqual((index, _id), (A.INCIDENT_INDEX, ALERT["alert_id"]))
        doc = body["doc"]
        self.assertEqual((doc["status"], doc["root_cause_category"], doc["how_detected"], doc["checked"], doc["confirmed_by"]),
                         ("confirmed", "db", "只有 lots 慢", ["Oracle AWR / 慢查詢"], "老王"))
        self.assertEqual(body["upsert"]["services"], ["mes-api"])     # 事故還沒開出來時也能補一筆
        self.assertEqual(self.es.ensured, [A.FEEDBACK_INDEX, A.INCIDENT_INDEX])   # 先用正式的 mapping 建 index

    def test_false_positive_keeps_reason_and_does_not_store_root_cause(self):
        r = A.submit(self.es, {"alert_id": ALERT["alert_id"], "verdict": "false_positive", "by": "amy", "fp_reason": "batch",
                               "fp_reason_text": "月底結帳", "root_cause": "不該存"}, NOW)
        self.assertEqual(r["incident"], "false_positive")
        fb = list(self.es.written[A.FEEDBACK_INDEX].values())
        self.assertEqual((fb[0]["fp_reason"], fb[0]["note"]), ("batch", "月底結帳"))
        self.assertNotIn("root_cause", self.es.updates[0][2]["doc"])

    def test_wrong_root_cause_records_correct_one(self):
        A.submit(self.es, {"alert_id": ALERT["alert_id"], "verdict": "wrong_root_cause", "by": "amy", "root_cause": "其實是網路", "root_cause_category": "network"}, NOW)
        fb = list(self.es.written[A.FEEDBACK_INDEX].values())
        self.assertEqual(fb[0]["correct_root_cause"], "其實是網路")
        self.assertEqual(self.es.updates[0][2]["doc"]["status"], "corrected")

    def test_anomalies_gone_still_one_feedback_row(self):
        self.es.anoms = {}
        r = A.submit(self.es, {"alert_id": ALERT["alert_id"], "verdict": "true_positive", "by": "amy"}, NOW)
        self.assertEqual(r["feedback"], 1)

    def test_bad_input(self):
        with self.assertRaises(KeyError):
            A.submit(self.es, {"alert_id": "nope", "verdict": "true_positive"}, NOW)
        with self.assertRaises(ValueError):
            A.submit(self.es, {"alert_id": ALERT["alert_id"], "verdict": "maybe"}, NOW)
        self.assertEqual(self.es.written, {})


if __name__ == "__main__":
    unittest.main()

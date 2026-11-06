"""回饋入口（api.app）：表單預填、送出後寫 aiops-feedback（每筆異常一筆）和 aiops-incidents（狀態、老師傅的經驗）。假的 ES。"""
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from shared.infra.es import doc_id
import interface.web.dto as D
import learn.feedback_service as F
import interface.web.views as V
from incident.store.models import ALERT_INDEX
from learn.store.models import FEEDBACK_INDEX
from shared.types.incident import INCIDENT_INDEX
from shared.infra.repos import Repos

NOW =datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)
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
        if path.startswith(f"/{ALERT_INDEX}/_doc/"):
            _id = path.rsplit("/", 1)[1]
            return {"found": _id in self.alerts, "_source": self.alerts.get(_id)}
        if "_mget" in path:
            return {"docs": [{"_id": i, "found": i in self.anoms, "_source": self.anoms.get(i)} for i in body["ids"]]}
        if "_update/" in path:
            self.updates.append((path.split("/")[1], path.split("/")[3].split("?")[0], body))
            return {}
        if path.endswith("_refresh"):
            return {}
        if method == "PUT" and path.startswith(f"/{FEEDBACK_INDEX}/_doc/"):     # 漏報：單筆 PUT，_id 在路徑裡
            self.written.setdefault(FEEDBACK_INDEX, {})[path.rsplit("/", 1)[1].split("?")[0]] = body
            return {}
        if method == "PUT" and path.startswith("/aiops-tuning/_doc/"):    # 一句話抽出來的建議（proposed）
            self.written.setdefault("aiops-tuning", {})[path.split("/")[3].split("?")[0]] = body
            return {}
        raise AssertionError(path)

    def ensure_index(self, name, body):
        self.ensured = getattr(self, "ensured", []) + [name]

    def bulk_index(self, index, docs):
        self.written.setdefault(index, {}).update(dict(docs))


class FormData(unittest.TestCase):
    def test_prefill(self):
        d = D.form_data(ALERT)
        self.assertEqual(d["alert_id"], ALERT["alert_id"])
        self.assertEqual(len(d["similar"]), 1)
        html = V.render_form(d)
        for s in ("mes-api 變慢", "統計資訊過期", "怎麼看出來的", 'name="root_cause_category"', 'value="db"', "AI 摘要", "/report?alert="):
            self.assertIn(s, html)

    def test_report_page(self):
        self.assertIn("# 報告", V.render_report(ALERT))

    def test_full_puts_report_above_the_form(self):
        # Grafana 抽屜：根因全文在上、老師傅回饋表單在最下面，送出後還是 full（hidden 欄位帶著）
        html = V.render_form(D.form_data(ALERT), full=True)
        self.assertLess(html.index("根因分析全文"), html.index("# 報告"))
        self.assertLess(html.index("# 報告"), html.index("老師傅）回饋"))
        self.assertLess(html.index("老師傅）回饋"), html.index('<form method="post" action="/feedback"'))
        self.assertIn('name="full" value="1"', html)
        self.assertNotIn("/report?alert=", html)
        self.assertNotIn("根因分析全文", V.render_form(D.form_data(ALERT)))


class Submit(unittest.TestCase):
    def setUp(self):
        self.es = FakeES()

    def test_true_positive_writes_feedback_per_anomaly_and_confirms_incident(self):
        r = F.submit(Repos(self.es), {"alert_id": ALERT["alert_id"], "verdict": "true_positive", "by": "老王", "root_cause_category": "db",
                               "root_cause": "統計資訊過期", "action": "重跑 analyze", "how_detected": "只有 lots 慢",
                               "checked": ["Oracle AWR / 慢查詢", ""], "improve": "加一條 Oracle 的規則"}, NOW)
        self.assertEqual(r, {"feedback": 2, "incident": "confirmed", "by": "老王"})
        fb = list(self.es.written[FEEDBACK_INDEX].values())
        self.assertEqual(sorted(f["anomaly_id"] for f in fb), ["x1", "x2"])     # 不存在的 gone 不寫
        self.assertTrue(all(f["alert_id"] == ALERT["alert_id"] and f["verdict"] == "true_positive" for f in fb))
        self.assertEqual(fb[0]["series"], "http.latency_p95_s")
        (index, _id, body), = self.es.updates
        self.assertEqual((index, _id), (INCIDENT_INDEX, ALERT["alert_id"]))
        doc = body["doc"]
        self.assertEqual((doc["status"], doc["root_cause_category"], doc["how_detected"], doc["checked"], doc["confirmed_by"]),
                         ("confirmed", "db", "只有 lots 慢", ["Oracle AWR / 慢查詢"], "老王"))
        self.assertEqual(body["upsert"]["services"], ["mes-api"])     # 事故還沒開出來時也能補一筆
        self.assertEqual(self.es.ensured, [FEEDBACK_INDEX, INCIDENT_INDEX])   # 先用正式的 mapping 建 index

    def test_false_positive_keeps_reason_and_does_not_store_root_cause(self):
        r = F.submit(Repos(self.es), {"alert_id": ALERT["alert_id"], "verdict": "false_positive", "by": "amy", "fp_reason": "batch",
                               "fp_reason_text": "月底結帳", "root_cause": "不該存"}, NOW)
        self.assertEqual(r["incident"], "false_positive")
        fb = list(self.es.written[FEEDBACK_INDEX].values())
        self.assertEqual((fb[0]["fp_reason"], fb[0]["note"]), ("batch", "月底結帳"))
        self.assertNotIn("root_cause", self.es.updates[0][2]["doc"])

    def test_wrong_root_cause_records_correct_one(self):
        F.submit(Repos(self.es), {"alert_id": ALERT["alert_id"], "verdict": "wrong_root_cause", "by": "amy", "root_cause": "其實是網路", "root_cause_category": "network"}, NOW)
        fb = list(self.es.written[FEEDBACK_INDEX].values())
        self.assertEqual(fb[0]["correct_root_cause"], "其實是網路")
        self.assertEqual(self.es.updates[0][2]["doc"]["status"], "corrected")

    def test_anomalies_gone_still_one_feedback_row(self):
        self.es.anoms = {}
        r = F.submit(Repos(self.es), {"alert_id": ALERT["alert_id"], "verdict": "true_positive", "by": "amy"}, NOW)
        self.assertEqual(r["feedback"], 1)

    def test_bad_input(self):
        with self.assertRaises(KeyError):
            F.submit(Repos(self.es), {"alert_id": "nope", "verdict": "true_positive"}, NOW)
        with self.assertRaises(ValueError):
            F.submit(Repos(self.es), {"alert_id": ALERT["alert_id"], "verdict": "maybe"}, NOW)
        self.assertEqual(self.es.written, {})


class SubmitMissed(unittest.TestCase):
    """漏報：沒有告警、沒有異常，直接寫一筆 verdict: missed；格式、_id 要跟指令 feedback add --verdict missed 一樣。"""
    def setUp(self):
        self.es = FakeES()
        self.form = {"series": "http.latency_p95_s", "service": "mes-api", "at": "2026-10-25 10:00", "by": "老王", "note": "lots 查詢全逾時"}

    def test_naive_time_is_taipei_and_id_matches_cli(self):
        r = F.submit_missed(Repos(self.es), self.form, NOW)
        self.assertEqual(r, {"feedback": 1, "verdict": "missed", "series": "http.latency_p95_s", "service": "mes-api", "by": "老王"})
        (_id, doc), = self.es.written[FEEDBACK_INDEX].items()
        self.assertEqual(doc["anomaly_at"], "2026-10-25T10:00:00+08:00")     # 沒寫時區 → 當工廠當地時間，跟指令一樣
        self.assertEqual((doc["verdict"], doc["series"], doc["keys"], doc["by"], doc["note"]),
                         ("missed", "http.latency_p95_s", {"service": "mes-api"}, "老王", "lots 查詢全逾時"))
        self.assertNotIn("anomaly_id", doc)
        self.assertEqual(_id, doc_id("feedback", ["http.latency_p95_s", {"service": "mes-api"}, "2026-10-25T10:00:00+08:00"], "老王"))   # cmd_feedback.py 的算法
        self.assertEqual(self.es.ensured[0], FEEDBACK_INDEX)      # 先用正式的 mapping 建 index

    def test_tz_aware_time_kept_and_optional_keys(self):
        F.submit_missed(Repos(self.es), {**self.form, "at": "2026-10-25T02:00:00.000Z", "route": "/api/lots", "pod": "", "by": ""}, NOW)
        (doc,) = self.es.written[FEEDBACK_INDEX].values()
        self.assertEqual(doc["anomaly_at"], "2026-10-25T02:00:00+00:00")     # Grafana 的 datetime 元素送的是 UTC ISO
        self.assertEqual(doc["keys"], {"service": "mes-api", "route": "/api/lots"})   # 空的 pod 不進 keys
        self.assertEqual(doc["by"], "anonymous")

    def test_series_comes_from_series_yaml(self):
        self.assertIn("http.latency_p95_s", F.SERIES_NAMES)
        self.assertTrue(len(F.SERIES_NAMES) >= 5)

    def test_bad_input(self):
        for bad, msg in (({"series": ""}, "series 要是"), ({"series": "nope.series"}, "http.latency_p95_s"), ({"service": " "}, "service 要填"),
                         ({"at": ""}, "at 要填"), ({"at": "昨天"}, "at 看不懂")):
            with self.assertRaises(ValueError, msg=bad) as cm:
                F.submit_missed(Repos(self.es), {**self.form, **bad}, NOW)
            self.assertIn(msg, str(cm.exception))
        self.assertEqual(self.es.written, {})
class FreeTextAndRatings(unittest.TestCase):
    """一句自然語言＋兩題評分（docs/pdca/phase5-learning-loop/2026-10-feedback-nl-extract.md）：抽成四欄存在回饋上、
    對得到調整種類就寫一筆 proposed 的 aiops-tuning；抽不到只存原文、建議寫不進去也不影響回饋。"""
    TEXT = "這是每月初的批次匯出報表，屬於預期內負載。"

    def setUp(self):
        self.es = FakeES()
        self.env = mock.patch.dict(os.environ, {"LLM_PROVIDER": "template"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def submit(self, **extra):
        return F.submit(Repos(self.es), {"alert_id": ALERT["alert_id"], "verdict": "false_positive", "by": "amy", "fp_reason": "batch", **extra}, NOW)

    def test_text_extracted_stored_and_tuning_proposed(self):
        r = self.submit(feedback_text=self.TEXT, rca_cause_useful="yes", rca_action_useful="no")
        self.assertEqual(r["extract"]["suggested_action"], "suppress_during_cron")
        self.assertTrue(r["extract"]["ok"] and r["tuning_id"])
        for fb in self.es.written[FEEDBACK_INDEX].values():        # 一件的每筆異常各一筆，都帶原文、抽取結果、兩題、建議 id
            self.assertEqual(fb["feedback_text"], self.TEXT)
            self.assertEqual(fb["extract"]["identified_cause"], "scheduled_batch_job")
            self.assertEqual((fb["rca_cause_useful"], fb["rca_action_useful"], fb["tuning_id"]), ("yes", "no", r["tuning_id"]))
        tun = self.es.written["aiops-tuning"][r["tuning_id"]]
        self.assertEqual((tun["kind"], tun["status"], tun["source"], tun["effect"]), ("recurring_override", "proposed", "feedback", {"mute": True}))
        self.assertNotIn("schedule", tun)                              # 文字沒寫時段就不猜
        self.assertIn(self.TEXT, tun["reason"])
        self.assertIn("缺 schedule", tun["reason"])
        self.assertEqual(tun["match"], {"service": "^mes\\-api$"})
        self.assertEqual(tun["by"], "amy")

    def test_action_none_writes_no_tuning(self):
        r = F.submit(Repos(self.es), {"alert_id": ALERT["alert_id"], "verdict": "true_positive", "by": "amy", "feedback_text": "這件是真的，但不用調，處理完了。"}, NOW)
        self.assertTrue(r["extract"]["ok"])
        self.assertIsNone(r["tuning_id"])
        self.assertNotIn("aiops-tuning", self.es.written)

    def test_extraction_failure_keeps_text_only(self):
        with mock.patch.object(F.LLM, "extract_feedback", return_value={"ok": False, "fields": None, "provider": "openai", "reason": "回的不是 JSON", "raw": "x"}):
            r = self.submit(feedback_text=self.TEXT)
        self.assertEqual(r["incident"], "false_positive")                # 回饋照寫
        self.assertIsNone(r["tuning_id"])
        fb = next(iter(self.es.written[FEEDBACK_INDEX].values()))
        self.assertEqual((fb["feedback_text"], fb["extract"]["ok"], fb["extract"]["reason"]), (self.TEXT, False, "回的不是 JSON"))
        self.assertNotIn("feedback_type", fb["extract"])
        self.assertNotIn("aiops-tuning", self.es.written)

    def test_tuning_write_failure_does_not_fail_feedback(self):
        with mock.patch.object(F, "propose_from_text", side_effect=RuntimeError("PUT /aiops-tuning → 403")):
            r = self.submit(feedback_text=self.TEXT)
        self.assertEqual(r["feedback"], 2)
        self.assertIsNone(r["tuning_id"])
        self.assertIn("403", r["tuning_error"])

    def test_no_text_response_unchanged_and_bad_rating_ignored(self):
        r = self.submit(rca_cause_useful="maybe")
        self.assertEqual(r, {"feedback": 2, "incident": "false_positive", "by": "amy"})
        fb = next(iter(self.es.written[FEEDBACK_INDEX].values()))
        self.assertNotIn("rca_cause_useful", fb)
        self.assertNotIn("feedback_text", fb)

    def test_form_has_text_and_ratings(self):
        html = V.render_form(D.form_data(ALERT))
        for s in ('name="feedback_text"', 'name="rca_cause_useful"', 'name="rca_action_useful"', 'value="not_seen"', "可能根因有參考價值", "建議的排查動作有參考價值"):
            self.assertIn(s, html)
        done = V.render_form(D.form_data(ALERT), done={"feedback": 2, "incident": "false_positive", "by": "amy",
                                                        "extract": {"ok": True, "suggested_action": "suppress_during_cron"}, "tuning_id": "t1"})
        self.assertIn("調整建議（待審核，id t1）", done)


if __name__ == "__main__":
    unittest.main()

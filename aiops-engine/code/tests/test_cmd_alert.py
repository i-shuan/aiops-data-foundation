"""事件整合的兩個防護（docs/pdca/phase5-learning-loop/2026-10-fix-critical.md 1、3）：LLM 失敗不擋這一輪；已確認的事故不會被先讀再寫蓋掉。"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from investigator import cmd_alert as C

AL = {"alert_id": "a-1", "service": "mes-api", "started_at": "2026-10-25T02:00:00+00:00", "updated_at": "2026-10-25T02:12:00+00:00",
      "routes": [], "series": ["x"], "severity": "warning", "dept": "D3120"}
T0 = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)


def span(tid, sid, parent, service, name, kind, target=None, error=False):
    return {"trace_id": tid, "span_id": sid, "parent": parent, "service": service, "name": name, "kind": kind, "target": target,
            "start_ms": 0.0, "dur_ms": 10.0, "error": error, "exception": None, "links": [], "attrs": {}, "resource": {}}


class CallEdges(unittest.TestCase):
    """跨服務併件的「誰呼叫誰」（docs/pdca/phase3-locate/2026-10-cross-service-aggregation.md）：
    從候選服務的 trace 用根因分析同一套 topology 算邊，只留兩端都是候選服務的。"""

    SPANS = [  # mes-api GET /lots → wip-worker GET /wip → oracle；mes-report 自己查 oracle
        span("t1", "a", None, "mes-api", "GET /lots/{id}", "Server"),
        span("t1", "b", "a", "mes-api", "GET", "Client", target="wip-worker"),
        span("t1", "c", "b", "wip-worker", "GET /wip", "Server"),
        span("t1", "d", "c", "wip-worker", "SELECT LOT_HISTORY", "Client", target="oracle"),
        span("t2", "e", None, "mes-report", "GET /report", "Server"),
        span("t2", "f", "e", "mes-report", "SELECT", "Client", target="oracle"),
    ]

    def test_edges_between_candidate_services_only(self):
        ids = {"mes-api": ["t1"], "wip-worker": ["t1"], "mes-report": ["t2"]}
        asked = []
        with mock.patch.object(C.E, "service_trace_ids", side_effect=lambda es, ix, s, a, b, n: (asked.append(s), ids.get(s, []))[1]), \
             mock.patch.object(C.T, "spans_of", return_value=self.SPANS) as spans_of:
            edges = C.call_edges("es", {"indices": {"traces": "traces-*"}}, {"mes-api", "wip-worker", "mes-report"}, T0, T0 + timedelta(hours=1), per=50)
        self.assertEqual(edges, {("mes-api", "wip-worker")})       # oracle 不是候選服務；mes-report 沒人呼叫、也沒呼叫誰
        self.assertEqual(sorted(asked), ["mes-api", "mes-report", "wip-worker"])
        self.assertEqual(spans_of.call_args[0][2], ["t1", "t2"])     # 同一個 trace 只讀一次

    def test_fewer_than_two_services_or_no_traces(self):
        self.assertEqual(C.call_edges("es", {}, {"mes-api"}, T0, T0), set())
        with mock.patch.object(C.E, "service_trace_ids", return_value=[]):
            self.assertEqual(C.call_edges("es", {"indices": {"traces": "traces-*"}}, {"mes-api", "wip-worker"}, T0, T0), set())


class IncidentKeepsDownstreamOnly(unittest.TestCase):
    """連帶的上游不進事故的 services（feedback 的規則建議依 services 寫 match，根因在下游）；另外記 upstream_services。"""

    def test_services_and_upstream(self):
        al = {"alert_id": "a-1", "service": "wip-worker", "upstream_services": ["mes-api"], "severity": "critical",
              "anomaly_ids": ["x1", "x2"], "routes": [], "series": []}
        doc = {"@timestamp": "t", "started_at": "s", "updated_at": "u", "title": "x", "dept": None, "group": "g"}
        inc = C._incident_doc(al, doc, None)
        self.assertEqual((inc["services"], inc["upstream_services"]), (["wip-worker"], ["mes-api"]))
        for f in ("upstream_services", "merged_into"):
            self.assertEqual(C.ALERT_MAPPING["mappings"]["properties"][f], {"type": "keyword"})


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


class FeedbackFooter(unittest.TestCase):
    def test_footer_added_once(self):
        url = "http://fb/feedback?alert=a-1"
        r = C.with_feedback_footer("# 根因分析\n\n## 結論\n- x\n", url)
        self.assertTrue(r.endswith(f"---\n資深維運專家（老師傅）回饋：{url}\n"))
        self.assertEqual(C.with_feedback_footer(r, url), r)          # 下一輪寫回去不會再加一行
        self.assertEqual(C.with_feedback_footer("報告", None), "報告")  # 沒設 feedback_url 不加
        self.assertIsNone(C.with_feedback_footer(None, url))


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

"""事件整合的兩個防護（docs/pdca/phase5-learning-loop/2026-10-fix-critical.md 1、3）：LLM 失敗不擋這一輪；已確認的事故不會被先讀再寫蓋掉。"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import incident.alert_service as C
from shared.infra.repos import Repos
from shared.scenario import load_scenario

SC = load_scenario()
from incident.store.models import ALERT_MAPPING
import incident.strategy.it_topology as CH

AL = C.Alert(alert_id="a-1", category="service", service="mes-api", status="open", started_at="2026-10-25T02:00:00+00:00",
             updated_at="2026-10-25T02:12:00+00:00", routes=[], series=["x"], severity="warning", dept="D3120")
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
        traces = mock.Mock(service_trace_ids=mock.Mock(side_effect=lambda ix, s, a, b, n: (asked.append(s), ids.get(s, []))[1]),
                           spans=mock.Mock(return_value=self.SPANS))
        edges = CH.call_edges(mock.Mock(traces=traces), {"indices": {"traces": "traces-*"}}, {"mes-api", "wip-worker", "mes-report"}, T0, T0 + timedelta(hours=1), per=50)
        self.assertEqual(edges, {("mes-api", "wip-worker")})       # oracle 不是候選服務；mes-report 沒人呼叫、也沒呼叫誰
        self.assertEqual(sorted(asked), ["mes-api", "mes-report", "wip-worker"])
        self.assertEqual(traces.spans.call_args[0][1], ["t1", "t2"])     # 同一個 trace 只讀一次

    def test_fewer_than_two_services_or_no_traces(self):
        self.assertEqual(CH.call_edges(mock.Mock(), {}, {"mes-api"}, T0, T0), set())
        repo = mock.Mock(traces=mock.Mock(service_trace_ids=mock.Mock(return_value=[])))
        self.assertEqual(CH.call_edges(repo, {"indices": {"traces": "traces-*"}}, {"mes-api", "wip-worker"}, T0, T0), set())


class IncidentKeepsDownstreamOnly(unittest.TestCase):
    """連帶的上游不進事故的 services（feedback 的規則建議依 services 寫 match，根因在下游）；另外記 upstream_services。"""

    def test_services_and_upstream(self):
        al = C.Alert(alert_id="a-1", category="service", service="wip-worker", status="open", started_at=T0, updated_at=T0,
                     upstream_services=["mes-api"], severity="critical", anomaly_ids=["x1", "x2"], routes=[], series=[])
        doc = {"@timestamp": "t", "started_at": "s", "updated_at": "u", "title": "x", "dept": None, "group": "g"}
        inc = C.Incident.from_alert(al, doc, None)
        self.assertEqual((inc.services, inc.upstream_services), (["wip-worker"], ["mes-api"]))
        for f in ("upstream_services", "merged_into"):
            self.assertEqual(ALERT_MAPPING["mappings"]["properties"][f], {"type": "keyword"})


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
        n = Repos(es, SC).incidents.upsert_auto([("a-1", {"status": "auto", "title": "x"}), ("a-2", {"status": "auto", "title": "y"})])
        self.assertEqual(n, 1)
        lines = [json.loads(l) for l in es.s.sent.strip().split("\n")]
        self.assertEqual(lines[0], {"update": {"_index": "aiops-incidents", "_id": "a-1"}})
        body = lines[1]
        # 判斷和寫入在 ES 裡一次做：狀態不是 auto 就 ctx.op = 'none'，一個欄位都不動；不存在就 upsert
        self.assertIn("ctx._source.status == 'auto'", body["script"]["source"])
        self.assertIn("ctx.op = 'none'", body["script"]["source"])
        self.assertEqual(body["script"]["params"]["doc"], {"status": "auto", "title": "x"})
        self.assertEqual(body["upsert"], {"status": "auto", "title": "x"})
        self.assertEqual(es.ensured, ["aiops-incidents"])

    def test_bulk_error_raises(self):
        es = FakeES({"errors": True, "items": [{"update": {"_id": "a-1", "error": {"reason": "bad script"}}}]})
        with self.assertRaises(RuntimeError):
            Repos(es, SC).incidents.upsert_auto([("a-1", {"status": "auto"})])

    def test_empty(self):
        self.assertEqual(Repos(FakeES({})).incidents.upsert_auto([]), 0)


class AnomaliesCarryAlertId(unittest.TestCase):
    """併件時把 alert_id 寫回 aiops-anomalies：儀表板「這件事故的異常」依 alert_id 撈，規則觸發的（沒有 status）也撈得到。"""

    def test_bulk_update_alert_id(self):
        es = FakeES({"errors": False, "items": [{"update": {"_id": "x1", "result": "updated"}}, {"update": {"_id": "x2", "result": "noop"}}]})
        n = Repos(es, SC).anomalies.tag_alert([("x1", "a-1"), ("x2", "a-1")])
        self.assertEqual(n, 2)
        lines = [json.loads(l) for l in es.s.sent.strip().split("\n")]
        self.assertEqual(lines[0], {"update": {"_index": "aiops-anomalies", "_id": "x1"}})
        self.assertEqual(lines[1], {"doc": {"alert_id": "a-1"}})
        self.assertEqual(es.ensured, ["aiops-anomalies"])   # 舊 index 補 alert_id 欄位（mapping 是 dynamic false）

    def test_missing_anomaly_is_skipped(self):
        # 保留期過了、異常已刪：那一筆 document_missing，其他照寫，不擋這一輪
        es = FakeES({"errors": True, "items": [{"update": {"_id": "x1", "error": {"type": "document_missing_exception"}}},
                                               {"update": {"_id": "x2", "result": "updated"}}]})
        self.assertEqual(Repos(es, SC).anomalies.tag_alert([("x1", "a-1"), ("x2", "a-1")]), 1)

    def test_other_error_raises(self):
        es = FakeES({"errors": True, "items": [{"update": {"_id": "x1", "error": {"type": "index_closed_exception"}}}]})
        with self.assertRaises(RuntimeError):
            Repos(es, SC).anomalies.tag_alert([("x1", "a-1")])

    def test_empty(self):
        es = FakeES({})
        self.assertEqual(Repos(es, SC).anomalies.tag_alert([]), 0)
        self.assertEqual(es.ensured, [])


if __name__ == "__main__":
    unittest.main()


class RerunWithoutTrace(unittest.TestCase):
    """開件太快、trace 還沒合成完時第一份報告是空的：沒有 trace 的件隔一陣子重跑，最多幾次。"""
    NOW = datetime(2026, 11, 1, 12, 50, tzinfo=timezone.utc)
    ACFG = {"rca_retry_after": "2m", "rca_retry_max": 5}

    def al(self, **kw):
        base = {"alert_id": "a", "category": "service", "service": "s", "status": "open", "started_at": self.NOW, "updated_at": self.NOW,
                "report": "# 報告", "findings": {"requests": {"requests": 0}}, "report_at": (self.NOW - timedelta(minutes=3)).isoformat(),
                "rca_attempts": 1}
        return C.Alert(**{**base, **kw})

    def test_no_trace_and_old_enough_reruns(self):
        self.assertTrue(C.needs_rerun(self.al(), self.NOW, self.ACFG))

    def test_too_soon_after_last_try(self):
        self.assertFalse(C.needs_rerun(self.al(report_at=(self.NOW - timedelta(seconds=60)).isoformat()), self.NOW, self.ACFG))

    def test_has_trace_no_rerun(self):
        self.assertFalse(C.needs_rerun(self.al(findings={"requests": {"requests": 120}}), self.NOW, self.ACFG))

    def test_stops_after_max_attempts(self):
        self.assertFalse(C.needs_rerun(self.al(rca_attempts=5), self.NOW, self.ACFG))
        self.assertTrue(C.needs_rerun(self.al(rca_attempts=4), self.NOW, self.ACFG))

    def test_new_alert_or_no_report_is_not_a_rerun(self):
        self.assertFalse(C.needs_rerun(self.al(new=True), self.NOW, self.ACFG))       # 新的一件走原本的第一次分析
        self.assertFalse(C.needs_rerun(self.al(report=None), self.NOW, self.ACFG))

    def test_failed_analysis_counts_as_no_trace(self):
        self.assertTrue(C.needs_rerun(self.al(findings=None), self.NOW, self.ACFG))   # 分析失敗（沒有 findings）也重試

    def test_old_alert_without_counter_counts_as_one_attempt(self):
        self.assertEqual(C.attempts(self.al(rca_attempts=None)), 1)
        self.assertEqual(C.attempts(self.al(rca_attempts=None, report=None)), 0)

    def test_can_be_turned_off(self):
        self.assertFalse(C.needs_rerun(self.al(), self.NOW, {**self.ACFG, "rca_retry": False}))


class FeedbackFooter(unittest.TestCase):
    def test_footer_added_once(self):
        url = "http://fb/feedback?alert=a-1"
        r = C.with_feedback_footer("# 根因分析\n\n## 結論\n- x\n", url)
        self.assertTrue(r.endswith(f"---\n資深維運專家（老師傅）回饋：{url}\n"))
        self.assertEqual(C.with_feedback_footer(r, url), r)          # 下一輪寫回去不會再加一行
        self.assertEqual(C.with_feedback_footer("報告", None), "報告")  # 沒設 feedback_url 不加
        self.assertIsNone(C.with_feedback_footer(None, url))


class Titled(unittest.TestCase):
    def test_first_line_is_title_then_report(self):
        t = C.titled("[critical] mes-api：錯誤率", "# 根因分析\n- x")
        self.assertEqual(t.split("\n", 1)[0], "[critical] mes-api：錯誤率")
        self.assertTrue(t.endswith("【根因分析】\n# 根因分析\n- x"))
        self.assertEqual(C.titled("只有標題", None), "只有標題")

    def test_llm_section_then_report_without_duplicate_quote_feedback_last(self):
        llm = "Oracle 那段變慢 [S1]。\n先看連線池 [E1]。"
        report = C.with_feedback_footer("> Oracle 那段變慢 [S1]。\n> 先看連線池 [E1]。\n\n# 根因分析\n- x\n", "http://fb/feedback?alert=a-1")
        t = C.titled("[critical] mes-api", report, llm)
        self.assertLess(t.index("【AI 摘要"), t.index("【根因分析】"))
        self.assertIn("【AI 摘要（LLM，只是參考）】\nOracle 那段變慢 [S1]。\n先看連線池 [E1]。", t)
        self.assertNotIn("> Oracle", t)                                   # 全文最前面的引用拿掉，不重複
        self.assertTrue(t.rstrip().endswith("資深維運專家（老師傅）回饋：http://fb/feedback?alert=a-1"))


class AlertDocUsesScenario(unittest.TestCase):
    """寫進 ES 前的那份文件：有變的件用情境重寫標題與摘要（S2 以後情境是傳進來的，loop_e2e 抓到過漏傳）。"""

    def test_title_summary_and_links(self):
        al = C.Alert(alert_id="a-1", category="service", service="mes-api", status="open", started_at=T0, updated_at=T0, severity="high",
                     anomaly_ids=["x1"], series=["http.error_rate"], routes=[], new=True,
                     anoms=[{"_id": "x1", "@timestamp": T0.isoformat(), "series": "http.error_rate", "keys": {"service": "mes-api"}, "value": 0.3, "median": 0.02}])
        doc = C.alert_doc(al, {"report_url": "http://r/{alert_id}", "feedback_url": "http://f/{alert_id}"}, T0 + timedelta(minutes=5),
                          timezone.utc, set(), load_scenario("observability"))
        self.assertTrue(doc["title"].startswith("[high] mes-api：http.error_rate 0.3（平常 0.02）"))
        self.assertEqual(doc["summary"].splitlines()[0], "[high] mes-api：http.error_rate 0.3（平常 0.02）")
        self.assertEqual((doc["report_url"], doc["feedback_url"], doc["anomaly_count"]), ("http://r/a-1", "http://f/a-1", 1))
        self.assertEqual(doc["@timestamp"], C.stamp_of(T0 + timedelta(minutes=5)).isoformat())
        self.assertNotIn("anoms", doc)


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

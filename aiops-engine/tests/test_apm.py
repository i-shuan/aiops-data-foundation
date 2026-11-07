"""資料來源是 Elastic APM 時（AIOPS_SOURCE=apm、traces-apm-*）：span 的轉換、查詢用的欄位、序列的 scale／div、owner 與 log 的欄位。
不連 ES（docs/pdca/meta/2026-11-apm-source.md）。"""
from tests.config_factory import series_of
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from common.scenario import load_scenario

import data.store.models as T
from data.source import to_span
import data.store.owner_repo as O
from common.infra.repos import Repos
from common.scenario import load_scenario

SC = load_scenario()
import data.trace.span_merge as M
from detect.utils import value_kind
from data.store.series_repo import _bucket_events, _bucket_value, _value_aggs

TX = {"@timestamp": "2026-11-02T01:00:00.000Z", "timestamp.us": 1793581200000000, "processor": {"event": "transaction"},
      "trace": {"id": "t1"}, "transaction": {"id": "tx1", "name": "GET /lots/{id}", "type": "request", "duration": {"us": 300000}, "result": "HTTP 2xx"},
      "event": {"outcome": "success"}, "service": {"name": "mes-api", "version": "1.2", "environment": "prod"},
      "kubernetes": {"pod": {"name": "mes-api-0"}, "namespace": "mes-prod"}, "host": {"name": "node-1"},
      "labels": {"dept": "D3120", "group": "it", "lot_id": "L001"}, "data_stream": {"namespace": "default"},
      "http": {"request": {"method": "GET"}, "response": {"status_code": 200}}, "url": {"path": "/lots/L001"}}
DB = {"@timestamp": "2026-11-02T01:00:00.030Z", "processor": {"event": "span"}, "trace": {"id": "t1"}, "parent": {"id": "tx1"},
      "transaction": {"id": "tx1"},
      "span": {"id": "s1", "name": "SELECT LOT_HISTORY", "type": "db", "subtype": "oracle", "action": "query", "duration": {"us": 150000},
               "destination": {"service": {"resource": "oracle"}}, "db": {"statement": "SELECT * FROM LOT_HISTORY WHERE id=?"}},
      "event": {"outcome": "failure"}, "service": {"name": "wip-worker"}}
HTTP = {"@timestamp": "2026-11-02T01:00:00.010Z", "processor": {"event": "span"}, "trace": {"id": "t1"}, "parent": {"id": "tx1"},
        "span": {"id": "s2", "name": "GET wip-worker", "type": "external", "subtype": "http", "duration": {"us": 250000},
                 "destination": {"service": {"resource": "wip-worker:8080"}}},
        "event": {"outcome": "success"}, "service": {"name": "mes-api"}}
APP = {"@timestamp": "2026-11-02T01:00:00.020Z", "processor": {"event": "span"}, "trace": {"id": "t1"}, "parent": {"id": "tx1"},
       "span": {"id": "s3", "name": "LotService#find", "type": "app", "duration": {"us": 5000}}, "event": {"outcome": "unknown"}, "service": {"name": "mes-api"}}


class ApmSpanTest(unittest.TestCase):
    """APM 的 transaction／span 文件轉成 core.traces 的 span 格式。"""

    def test_transaction_is_server_entry(self):
        s = to_span(TX)
        self.assertEqual((s["trace_id"], s["span_id"], s["parent"], s["service"], s["name"], s["kind"]),
                         ("t1", "tx1", None, "mes-api", "GET /lots/{id}", "Server"))
        self.assertEqual(s["dur_ms"], 300.0)                       # 微秒 → 毫秒
        self.assertEqual(s["start_ms"], 1793581200000.0)           # timestamp.us 比 @timestamp 準
        self.assertFalse(s["error"])
        self.assertEqual(s["resource"]["k8s.pod.name"], "mes-api-0")     # 攤成 OTel 的名字，rca.yaml 的 commonality 兩種來源同一套
        self.assertEqual((s["resource"]["dept"], s["resource"]["group"], s["resource"]["data_stream.namespace"]), ("D3120", "it", "default"))
        self.assertEqual(s["attrs"]["labels.lot_id"], "L001")
        self.assertEqual(s["attrs"]["http.response.status_code"], 200)

    def test_db_span_is_client_with_target_and_statement(self):
        s = to_span(DB)
        self.assertEqual((s["kind"], s["target"], s["parent"], s["error"]), ("Client", "oracle", "tx1", True))
        self.assertEqual(s["attrs"]["db.statement"], "SELECT * FROM LOT_HISTORY WHERE id=?")
        self.assertEqual(s["attrs"]["db.system"], "oracle")
        self.assertEqual(T.segment(s), "wip-worker › SELECT LOT_HISTORY → oracle")
        self.assertEqual(s["dur_ms"], 150.0)
        self.assertIsNone(s["exception"])                          # 例外是 logs-apm.error 的獨立文件，沒接

    def test_external_http_is_client_and_internal_span_has_no_target(self):
        self.assertEqual((to_span(HTTP)["kind"], to_span(HTTP)["target"]), ("Client", "wip-worker:8080"))
        self.assertEqual((to_span(APP)["kind"], to_span(APP)["target"]), ("Internal", None))

    def test_messaging(self):
        tx = {**TX, "transaction": {**TX["transaction"], "type": "messaging", "name": "orders process"}}
        self.assertEqual(to_span(tx)["kind"], "Consumer")
        pub = {**APP, "span": {**APP["span"], "type": "messaging", "subtype": "kafka", "action": "send"}}
        self.assertEqual(to_span(pub)["kind"], "Producer")

    def test_otel_doc_still_converts_the_old_way(self):
        s = to_span({"@timestamp": "2026-10-10T10:00:00Z", "trace_id": "t", "span_id": "s", "name": "SELECT X", "kind": "Client",
                       "duration": 2_000_000, "attributes": {"db.system.name": "oracle"}, "resource": {"attributes": {"service.name": "a"}}})
        self.assertEqual((s["target"], s["dur_ms"]), ("oracle", 2.0))

    def test_merge_builds_the_same_document_as_from_otel(self):
        spans = [to_span(d) for d in (TX, HTTP, APP, DB)]
        index, doc = T.merged_doc(spans, datetime(2026, 11, 2, 1, 5, tzinfo=timezone.utc))
        self.assertEqual(index, "traces-merged-it-2026.11.02")     # group 用 labels.group
        self.assertEqual(doc["root"], {"service": "mes-api", "name": "GET /lots/{id}", "kind": "Server", "error": False})
        self.assertEqual([e["span_id"] for e in doc["entries"]], ["tx1"])
        self.assertTrue(doc["error"])
        self.assertEqual(doc["services"], ["mes-api", "wip-worker"])
        self.assertEqual([s["span_id"] for s in T.error_origins(T.from_merged(doc))], ["s1"])


class ApmQueryTest(unittest.TestCase):
    """讀 traces-apm-* 時查詢用 APM 的欄位名。"""

    class FakeES:
        def __init__(self, hits):
            self.hits, self.bodies = hits, []

        def search(self, index, body):
            self.bodies.append((index, body))
            return {"hits": {"hits": [{"_source": h} for h in self.hits]}, "aggregations": {"t": {"buckets": [{"key": {"id": "t1"}}]}}}

    def test_schema_by_index_name(self):
        self.assertEqual([T.schema_of(i) for i in ("traces-apm-*", "traces-merged-*", "traces-generic.otel-*")], ["apm", "merged", "otel"])
        self.assertEqual(T.trace_id_field("traces-apm-*"), "trace.id")

    def test_spans_of_queries_trace_id_and_dedupes(self):
        es = self.FakeES([TX, DB, DB])
        got = Repos(es, load_scenario()).traces.spans("traces-apm-*", ["t1"], datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 11, 3, tzinfo=timezone.utc))
        self.assertEqual(sorted(s["span_id"] for s in got), ["s1", "tx1"])      # 同一毫秒跨頁重複的只留一個
        body = es.bodies[0][1]
        self.assertIn({"terms": {"trace.id": ["t1"]}}, body["query"]["bool"]["filter"])
        self.assertEqual(body["sort"], [{"@timestamp": "asc"}, {"_doc": "asc"}])
        self.assertIn("span.stacktrace", body["_source"]["excludes"])

    def test_spans_by_id_matches_span_or_transaction_id(self):
        es = self.FakeES([TX, DB])
        got = Repos(es, load_scenario()).traces.spans_by_id("traces-apm-*", ["tx1"], datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 11, 3, tzinfo=timezone.utc))
        self.assertEqual(list(got), ["tx1"])
        inner = next(f for f in es.bodies[0][1]["query"]["bool"]["filter"] if "bool" in f)       # 來源模組給的條件（SourceAdapter.span_ids_filter）
        should = inner["bool"]["should"]
        self.assertEqual(should, [{"terms": {"span.id": ["tx1"]}}, {"terms": {"transaction.id": ["tx1"]}}])

    def test_updated_trace_ids_aggregates_on_trace_dot_id(self):
        es = self.FakeES([])
        ids = Repos(es, load_scenario()).traces.updated_trace_ids("traces-apm-*", datetime(2026, 11, 2, tzinfo=timezone.utc), datetime(2026, 11, 2, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(ids, ["t1"])
        self.assertEqual(es.bodies[0][1]["aggs"]["t"]["composite"]["sources"], [{"id": {"terms": {"field": "trace.id"}}}])


class ScaleAndDivTest(unittest.TestCase):
    """序列的 scale（微秒 → 秒）與 div（兩個欄位的總和相除）。"""
    F = "transaction.duration.histogram"

    def test_percentile_scaled_to_seconds(self):
        v = {"percentile": {"field": self.F, "percent": 95}}
        self.assertEqual(_bucket_value(v, {"v": {"values": {"95.0": 250000.0}}, "n": {"value": 30}}, 1e-6), 0.25)
        self.assertIsNone(_bucket_value(v, {"v": {"values": {"95.0": None}}, "n": {"value": 0}}, 1e-6))

    def test_hist_ratio_above_threshold_converted_to_data_unit(self):
        v = {"hist_ratio_above": {"field": self.F, "gt": 2.0}}
        self.assertEqual(_value_aggs(v, 1e-6)["v"], {"percentile_ranks": {"field": self.F, "values": [2000000.0]}})
        self.assertAlmostEqual(_bucket_value(v, {"v": {"values": {"2000000.0": 97.5}}, "n": {"value": 400}}, 1e-6), 0.025)   # 比例不乘

    def test_ratios_and_counts_are_not_scaled(self):
        self.assertEqual(_bucket_value({"hist_count": self.F}, {"doc_count": 2, "v": {"value": 1260}}, 1e-6), 1260)
        v = {"hist_ratio": {"field": self.F, "query": {"term": {"event.outcome": "failure"}}}}
        self.assertAlmostEqual(_bucket_value(v, {"doc_count": 2, "v": {"value": 100}, "num": {"doc_count": 1, "v": {"value": 5}}}, 1e-6), 0.05)

    def test_div_average_latency(self):
        v = {"div": {"num": "span.destination.service.response_time.sum.us", "den": "span.destination.service.response_time.count"}}
        self.assertEqual(_value_aggs(v), {"den": {"sum": {"field": "span.destination.service.response_time.count"}},
                                          "num": {"sum": {"field": "span.destination.service.response_time.sum.us"}}})
        b = {"den": {"value": 7488.0}, "num": {"value": 21309833.0}}
        self.assertAlmostEqual(_bucket_value(v, b, 1e-6), 21309833 / 7488 / 1e6)
        self.assertEqual(_bucket_events(v, b), 7488.0)                 # 事件數是次數
        self.assertIsNone(_bucket_value(v, {"den": {"value": 0.0}, "num": {"value": 0.0}}))
        self.assertEqual(value_kind(series_of({"name": "x", "value": v})), "rate")

    def test_div_with_query_is_a_share_of_counts(self):
        v = {"div": {"num": {"field": "c", "query": {"term": {"event.outcome": "failure"}}}, "den": "c"}}
        aggs = _value_aggs(v)
        self.assertEqual(aggs["num"], {"filter": {"term": {"event.outcome": "failure"}}, "aggs": {"v": {"sum": {"field": "c"}}}})
        self.assertAlmostEqual(_bucket_value(v, {"den": {"value": 200.0}, "num": {"doc_count": 3, "v": {"value": 10.0}}}), 0.05)


class SourceProfileTest(unittest.TestCase):
    """AIOPS_SOURCE=apm：owner 查 APM 的索引與欄位、根因分析讀 log 用 APM 的欄位。"""

    def test_default_is_otel_and_unknown_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIOPS_SOURCE", None)
            self.assertEqual(load_scenario().source, "otel")
        with mock.patch.dict(os.environ, {"AIOPS_SOURCE": "zipkin"}):
            with self.assertRaises(ValueError):
                load_scenario()

    def test_owner_query_uses_apm_fields(self):
        class Fake:
            def __init__(self): self.calls = []
            def req(self, method, path, body):
                self.calls.append((path, body))
                return {"aggregations": {"ns": {"buckets": []}, "svc": {"buckets": [{"key": "mes-api", "d": {"buckets": [{"key": "D3120"}]}, "g": {"buckets": [{"key": "it"}]}}]}}}
        es = Fake()
        with mock.patch.dict(os.environ, {"AIOPS_SOURCE": "apm"}):
            repo = Repos(es, load_scenario())
            repo.anomalies.write([("a", {"@timestamp": "2026-11-01T00:00:00+00:00", "keys": {"service": "mes-api", "route": "GET /lots/{id}"}})])
        path, body = es.calls[0]
        self.assertTrue(path.startswith("/metrics-apm.transaction.1m-*/_search"))
        self.assertEqual(body["aggs"]["svc"]["terms"]["field"], "service.name")
        self.assertEqual(body["aggs"]["svc"]["aggs"]["d"]["terms"]["field"], "labels.dept")
        got = repo.anomalies.get("a")                                     # 異常寫進 PostgreSQL（tests/conftest.py 指好的）
        self.assertEqual((got["dept"], got["group"]), ("D3120", "it"))

    def test_owner_query_default_is_unchanged(self):
        class Fake:
            def req(self, method, path, body):
                self.path, self.body = path, body
                return {}
        es = Fake()
        with mock.patch.dict(os.environ, {"AIOPS_SOURCE": "otel"}):
            O.load_owners(es, load_scenario().owner)
        self.assertTrue(es.path.startswith("/metrics-*.otel-*/"))
        self.assertEqual(es.body["aggs"]["ns"]["terms"]["field"], "resource.attributes.k8s.namespace.name")

    def test_problem_logs_apm_fields_and_error_documents(self):
        class Fake:
            def search(self, index, body):
                self.body = body
                return {"hits": {"hits": [
                    {"fields": {"@timestamp": ["2026-11-02T01:00:00Z"], "service.name": ["mes-api"], "log.level": ["ERROR"], "trace.id": ["t1"],
                                "message": ["Connection pool timeout"]}},
                    {"fields": {"@timestamp": ["2026-11-02T01:00:01Z"], "service.name": ["mes-api"], "trace.id": ["t1"],
                                "error.exception.message": ["ORA-12170: TNS:Connect timeout"]}}]}}
        es = Fake()
        with mock.patch.dict(os.environ, {"AIOPS_SOURCE": "apm"}):
            logs = Repos(es, load_scenario()).logs.problems("logs-apm.app.*,logs-apm.error-*", ["mes-api"], datetime(2026, 11, 2, tzinfo=timezone.utc), datetime(2026, 11, 2, 1, tzinfo=timezone.utc))
        f = es.body["query"]["bool"]["filter"]
        self.assertIn({"terms": {"service.name": ["mes-api"]}}, f)
        which = f[2]["bool"]["should"]
        self.assertIn("ERROR", which[0]["terms"]["log.level"])
        self.assertEqual(which[1], {"term": {"processor.event": "error"}})       # 例外文件沒有 log.level，一起撈
        self.assertEqual([l["message"] for l in logs], ["Connection pool timeout", "ORA-12170: TNS:Connect timeout"])
        self.assertEqual(logs[0]["trace_id"], "t1")

    def test_logs_of_traces_apm_uses_trace_dot_id(self):
        class Fake:
            def search(self, index, body):
                self.body = body
                return {"hits": {"hits": []}}
        es = Fake()
        with mock.patch.dict(os.environ, {"AIOPS_SOURCE": "apm"}):
            Repos(es, load_scenario()).logs.of_traces("logs-apm.app.*", ["t1"], datetime(2026, 11, 2, tzinfo=timezone.utc), datetime(2026, 11, 2, 1, tzinfo=timezone.utc))
        self.assertIn({"terms": {"trace.id": ["t1"]}}, es.body["query"]["bool"]["filter"])
        self.assertIn("error.exception.message", es.body["fields"])

    def test_otel_logs_unchanged(self):
        class Fake:
            def search(self, index, body):
                self.body = body
                return {"hits": {"hits": []}}
        es = Fake()
        with mock.patch.dict(os.environ, {"AIOPS_SOURCE": "otel"}):
            Repos(es, load_scenario()).logs.problems("k8s-*", ["a"], datetime(2026, 11, 2, tzinfo=timezone.utc), datetime(2026, 11, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(es.body["fields"], ["@timestamp", "resource.attributes.service.name", "log.level", "trace_id", "message"])
        self.assertEqual(es.body["query"]["bool"]["filter"][2], {"terms": {"log.level": ["warn", "error", "fatal"]}})


if __name__ == "__main__":
    unittest.main()

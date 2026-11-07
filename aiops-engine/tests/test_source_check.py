"""data/source/check：宣告的來源跟資料對不對——只回報、不擋、自己不能變成故障。
為了確認：①宣告錯的三種樣子（格式錯、欄位錯、沒資料）都抓得到，並說得出可能的原因；②對的不報；③ES 連不上不丟例外；
④接進 detect／trace-merge 之後不擋它們、寫一筆 source-check-* 狀態文件給 Grafana 查。"""
import contextlib
import io
import unittest
from argparse import Namespace
from unittest import mock

import pandas as pd

import detect.detect_service as C
from common.infra.repos import Repos
from common.config import validate
from data.source import ADAPTERS, adapter_of
from data.source.check import check_fields, check_trace_index

APM_SPAN = {"@timestamp": "2026-11-07T03:00:00Z", "processor": {"event": "span"}, "trace": {"id": "t"}, "service": {"name": "mes-api"}}
OTEL_SPAN = {"@timestamp": "2026-11-07T03:00:00Z", "trace_id": "t", "span_id": "s", "resource": {"attributes": {"service.name": "mes-api"}}}
METRIC_APM = {"@timestamp": "2026-11-07T03:00:00Z", "service": {"name": "mes-api"}, "transaction": {"name": "GET /x"}}


class FakeES:
    """search 回事先給的文件；其他請求記下來。"""
    def __init__(self, docs=None, error=None):
        self.docs, self.error, self.written = docs or [], error, []

    def req(self, method, path, body=None, ok=None):
        if self.error:
            raise self.error
        if "_search" in path:
            return {"hits": {"hits": [{"_id": str(i), "_source": d} for i, d in enumerate(self.docs)]}}
        self.written.append((path, body))
        return {}

    def ensure_index(self, name, mapping):
        pass

    def search(self, index, body):
        return self.req("POST", f"/{index}/_search", body)

    def bulk_index(self, index, docs):
        self.written.append((index, docs))


class TraceIndex(unittest.TestCase):
    def test_declared_right_reports_nothing(self):
        self.assertEqual(check_trace_index(FakeES([OTEL_SPAN]), "traces-generic.otel-*", adapter_of("otel"), ADAPTERS), [])
        self.assertEqual(check_trace_index(FakeES([APM_SPAN]), "traces-apm-*", adapter_of("apm"), ADAPTERS), [])

    def test_declared_otel_but_data_is_apm(self):
        f, = check_trace_index(FakeES([APM_SPAN]), "traces-apm-*", adapter_of("otel"), ADAPTERS)
        self.assertIn("宣告的來源是 otel", f.problem)
        self.assertIn("長得像 apm", f.problem)                                  # 說得出「你可能該宣告 apm」

    def test_unknown_format_lists_the_fields_it_saw(self):
        f, = check_trace_index(FakeES([{"@timestamp": "x", "zipkin_id": "z"}]), "traces-zipkin-*", adapter_of("otel"), ADAPTERS)
        self.assertIn("zipkin_id", f.problem)

    def test_no_data(self):
        f, = check_trace_index(FakeES([]), "traces-typo-*", adapter_of("apm"), ADAPTERS)
        self.assertIn("沒有資料", f.problem)
        self.assertFalse(f.counts)

    def test_es_down_is_a_finding_not_an_exception(self):
        f, = check_trace_index(FakeES(error=ConnectionError("down")), "traces-*", adapter_of("apm"), ADAPTERS)
        self.assertIn("檢查不了", f.problem)


class SeriesFields(unittest.TestCase):
    def test_fields_present_in_some_doc_is_fine(self):
        self.assertEqual(check_fields(FakeES([METRIC_APM, {"service": {"name": "x"}}]), "a", "metrics-apm.*", None, ["service.name", "transaction.name"]), [])

    def test_otel_field_names_against_apm_data(self):
        f, = check_fields(FakeES([METRIC_APM]), "http.requests", "metrics-apm.*", None, ["resource.attributes.service.name", "attributes.http.route"])
        self.assertIn("resource.attributes.service.name", f.problem)
        self.assertIn("一次都沒出現", f.problem)

    def test_no_data_and_es_down(self):
        f, = check_fields(FakeES([]), "a", "metrics-*", None, ["x"])
        self.assertIn("沒有符合的資料", f.problem)
        self.assertFalse(f.counts)                                               # 抽不到資料不算對不上
        self.assertIn("檢查不了", check_fields(FakeES(error=TimeoutError("t")), "a", "metrics-*", None, ["x"])[0].problem)


CFG = validate({"interval": "5m", "series": [
    {"name": "a.cpu", "index": "metrics-apm.*", "by": ["resource.attributes.service.name"], "value": {"avg": "v"}},
    {"name": "a.mem", "index": "metrics-apm.*", "by": ["resource.attributes.service.name"], "value": {"avg": "w"}}]}, "series")


class WiredIntoDetect(unittest.TestCase):
    def run_detect(self, docs):
        es = FakeES(docs)
        with mock.patch.object(C, "ES", lambda: es), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = C.cmd_detect(CFG, Namespace(at=pd.Timestamp("2026-11-01T02:05:00Z"), window=pd.Timedelta("10min")))
        status = dict(Repos(None).alerts._select())        # 狀態文件寫進 PostgreSQL（tests/conftest.py 指好的）
        return rc, status, out.getvalue()

    def test_no_data_is_information_not_a_mismatch(self):
        _, status, text = self.run_detect([])                                    # 索引沒資料（例如沒開 log shipping）：印出來，但不叫人
        self.assertEqual(status["source-check-series"]["source_mismatch"], 0)
        self.assertIn("ℹ 來源檢查", text)

    def test_owner_index_empty_while_series_have_data_is_a_mismatch(self):
        """宣告錯來源最常見的樣子：序列（索引寫在 series.yaml 裡）查得到，歸屬的索引（依宣告的來源）卻沒資料。"""
        es = FakeES([METRIC_APM])
        real = es.req

        def req(method, path, body=None, ok=None):
            if "metrics-*.otel-*" in path:                                       # 歸屬的索引（宣告 otel）：沒資料
                return {"hits": {"hits": []}}
            return real(method, path, body, ok)
        es.req = req
        cfg = validate({"series": [{"name": "a", "index": "metrics-apm.*", "by": ["service.name"], "value": "count"}]}, "series")
        owner = {"index": "metrics-*.otel-*", "namespace": "resource.attributes.k8s.namespace.name", "service": "resource.attributes.service.name"}
        f, = C.source_findings(es, cfg, owner)
        self.assertTrue(f.counts)
        self.assertIn("宣告的來源", f.problem)
        # 序列也沒資料時（整個環境都沒資料）：不誇大，維持「抽不到資料」
        es2 = FakeES([])
        f2 = C.source_findings(es2, cfg, owner)
        self.assertTrue(all(not x.counts for x in f2))

    def test_mismatch_is_reported_but_detect_still_runs(self):
        rc, status, text = self.run_detect([METRIC_APM])
        self.assertEqual(rc, 0)                                                 # 不擋
        self.assertIn("來源檢查", text)
        doc = status["source-check-series"]
        # 兩個問題：序列（同索引同欄位的兩條只查一次、算一個）＋歸屬（宣告 otel，歸屬要的 resource.attributes.* 在 APM 資料裡沒有）
        self.assertEqual((doc["alert_id"], doc["category"], doc["status"], doc["source_mismatch"]), ("source-check-series", "status", "ok", 2))
        self.assertIn("resource.attributes.service.name", doc["summary"])
        self.assertIn("歸屬", doc["summary"])
        self.assertIn("a.cpu、a.mem", doc["summary"])

    def test_clean_run_writes_zero(self):
        _, status, text = self.run_detect([{"resource": {"attributes": {"service.name": "mes-api", "k8s.namespace.name": "mes"}}}])
        self.assertEqual(status["source-check-series"]["source_mismatch"], 0)
        self.assertNotIn("來源檢查", text)

    def test_a_crash_inside_the_check_never_stops_detect(self):
        with mock.patch.object(C, "source_findings", side_effect=RuntimeError("boom")):
            es = FakeES([METRIC_APM])
            with mock.patch.object(C, "ES", lambda: es), contextlib.redirect_stdout(io.StringIO()) as out:
                rc = C.cmd_detect(CFG, Namespace(at=pd.Timestamp("2026-11-01T02:05:00Z"), window=pd.Timedelta("10min")))
        self.assertEqual(rc, 0)
        self.assertIn("略過", out.getvalue())


if __name__ == "__main__":
    unittest.main()

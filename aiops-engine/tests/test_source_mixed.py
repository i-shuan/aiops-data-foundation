"""OTel 與 APM 並存（AIOPS_SOURCE=otel,apm）。為了確認：
1. 情境：兩份歸屬與 log 對照都載進來，主要的是第一個；寫錯會說清楚。
2. 歸屬：兩份各查各的索引、合併，同一個服務兩邊都有時主要的優先。
3. trace 合成：一個請求跨過兩套系統（A 用 APM、B 用 OTel）時，兩邊的 span 合成同一筆、呼叫鏈接得起來。
4. log：根因分析讀 log 時每個來源各查各的索引與欄位。"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from common.scenario import load_scenario
from data.store.owner_repo import load_all_owners
from data.trace import span_merge
from detect.config.trace import RawIndex
from common.config import ConfigError, validate
from incident.store.log_repo import LogRepository

T0 = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)


def env(**kw):
    return mock.patch.dict(os.environ, {"AIOPS_PLUGIN": "observability", **kw})


class MixedScenario(unittest.TestCase):
    def test_both_profiles_are_loaded_primary_first(self):
        with env(AIOPS_SOURCE="otel,apm"):
            sc = load_scenario()
        self.assertEqual((sc.source, sc.sources), ("otel", ("otel", "apm")))
        self.assertEqual(len(sc.owners), 2)
        self.assertEqual(sc.owners[0]["service"], "resource.attributes.service.name")      # 主要的：OTel
        self.assertEqual(sc.owners[1]["service"], "service.name")                          # 次要的：APM
        self.assertEqual(sc.logs[1]["index"], "logs-apm.app.*,logs-apm.error-*")           # 次要來源的 log 帶自己的索引

    def test_apm_primary(self):
        with env(AIOPS_SOURCE="apm,otel"):
            sc = load_scenario()
        self.assertEqual((sc.source, sc.owners[0]["service"], sc.logs[1]["index"]), ("apm", "service.name", "k8s-*"))

    def test_single_source_is_unchanged(self):
        with env(AIOPS_SOURCE="apm"):
            sc = load_scenario()
        self.assertEqual((sc.source, sc.sources, len(sc.owners), sc.extra_logs), ("apm", (), 1, ()))

    def test_typo_is_explained(self):
        with env(AIOPS_SOURCE="otel,zipkin"), self.assertRaises(ValueError) as cm:
            load_scenario()
        self.assertIn("zipkin", str(cm.exception))
        self.assertIn("otel,apm", str(cm.exception))


class FakeOwnerES:
    """每個索引回一份「服務 → dept、group」。"""
    def __init__(self, by_index):
        self.by_index = by_index

    def req(self, method, path, body=None, ok=None):
        index = path.split("/")[1].split("/")[0]
        rows = self.by_index.get(index, [])
        svc_field = next(iter(body["aggs"]["svc"]["terms"]["field"] for _ in [0]))
        bucket = lambda r: {"key": r[0], "d": {"buckets": [{"key": r[1]}]}, "g": {"buckets": [{"key": r[2]}]}}
        return {"aggregations": {"ns": {"buckets": []}, "svc": {"buckets": [bucket(r) for r in rows]}}}


class Owners(unittest.TestCase):
    def test_merged_and_primary_wins(self):
        es = FakeOwnerES({"metrics-*.otel-*": [("mes-api", "D1", "mes"), ("both", "D1", "primary")],
                          "metrics-apm.transaction.1m-*": [("wip-worker", "D2", "wip"), ("both", "D9", "secondary")]})
        with env(AIOPS_SOURCE="otel,apm"):
            sc = load_scenario()
        got = load_all_owners(es, sc.owners)
        self.assertEqual(got[("svc", "mes-api")], ("D1", "mes"))
        self.assertEqual(got[("svc", "wip-worker")], ("D2", "wip"))          # 次要來源的服務也查得到歸屬
        self.assertEqual(got[("svc", "both")], ("D1", "primary"))            # 兩邊都有：主要的優先


class FakeTraces:
    """兩個 raw 索引各有一部分 span；一個請求（trace t1）從 mes-api（APM）呼叫 wip-worker（OTel）。"""
    def __init__(self):
        self.calls, self.written = [], {}

    def updated_trace_ids(self, raw, since, until, source=None):
        self.calls.append(("ids", raw, source))
        return ["t1"]

    def spans(self, raw, ids, lo, hi, source=None):
        self.calls.append(("spans", raw, source))
        base = {"trace_id": "t1", "target": None, "links": [], "attrs": {}, "resource": {}, "exception": None, "error": False}
        if source == "apm":
            return [{**base, "span_id": "a1", "parent": None, "service": "mes-api", "name": "GET /lots", "kind": "Server", "start_ms": T0.timestamp() * 1000, "dur_ms": 100.0},
                    {**base, "span_id": "a2", "parent": "a1", "service": "mes-api", "name": "GET wip", "kind": "Client", "target": "wip-worker", "start_ms": T0.timestamp() * 1000 + 5, "dur_ms": 90.0}]
        return [{**base, "span_id": "o1", "parent": "a2", "service": "wip-worker", "name": "GET /wip", "kind": "Server", "start_ms": T0.timestamp() * 1000 + 8, "dur_ms": 80.0}]

    def write_merged(self, docs):
        self.written = docs
        return sum(len(v) for v in docs.values())


class Repo:
    def __init__(self):
        self.traces = FakeTraces()


class CrossSourceTrace(unittest.TestCase):
    def test_both_indices_are_read_and_merged_into_one_trace(self):
        repo = Repo()
        n = span_merge.merge(repo, [("traces-apm-*", "apm"), ("traces-generic.otel-*", "otel")], T0 - timedelta(minutes=1), T0, T0)
        self.assertEqual(n, 1)                                                 # 一個請求一筆，不是兩筆
        ((_, doc),) = next(iter(repo.traces.written.values()))
        self.assertEqual(sorted(s["service"] for s in doc["spans"]), ["mes-api", "mes-api", "wip-worker"])
        self.assertEqual({s["span_id"]: s["parent"] for s in doc["spans"]}, {"a1": None, "a2": "a1", "o1": "a2"})     # 呼叫鏈跨過兩個來源接得起來
        # 兩個索引各用自己明講的來源讀，而且每個索引的 span 都讀（不是只讀有新 span 的那個）
        self.assertIn(("spans", "traces-apm-*", "apm"), repo.traces.calls)
        self.assertIn(("spans", "traces-generic.otel-*", "otel"), repo.traces.calls)

    def test_a_single_index_string_still_works(self):
        repo = Repo()
        span_merge.merge(repo, "traces-apm-*", T0 - timedelta(minutes=1), T0, T0)
        self.assertEqual(repo.traces.calls[0], ("ids", "traces-apm-*", None))   # 沒明講來源：由索引名與宣告的來源決定（跟以前一樣）


class SeriesNames(unittest.TestCase):
    def test_duplicate_series_names_are_rejected_at_load(self):
        one = {"name": "http.requests", "index": "metrics-*.otel-*", "by": ["service"], "value": "count"}
        with self.assertRaises(ConfigError) as cm:
            validate({"series": [one, {**one, "index": "metrics-apm.*"}]}, "series")
        self.assertIn("http.requests", str(cm.exception))
        self.assertIn("來源前綴", str(cm.exception))
        validate({"series": [{**one, "name": "otel.http.requests"}, {**one, "name": "apm.http.requests", "index": "metrics-apm.*"}]}, "series")   # 加前綴就行


class TraceConfigRawIndexes(unittest.TestCase):
    def test_string_and_list_forms(self):
        self.assertEqual(validate({"raw_index": "traces-apm-*"}, "trace").raw_indexes(), [("traces-apm-*", None)])
        cfg = validate({"raw_index": [{"index": "traces-apm-*", "source": "apm"}, {"index": "traces-generic.otel-*", "source": "otel"}]}, "trace")
        self.assertEqual(cfg.raw_indexes(), [("traces-apm-*", "apm"), ("traces-generic.otel-*", "otel")])

    def test_unknown_source_is_rejected_at_load(self):
        with self.assertRaises(ConfigError) as cm:
            validate({"raw_index": [{"index": "traces-x-*", "source": "zipkin"}]}, "trace")
        self.assertIn("zipkin", str(cm.exception))
        self.assertEqual(RawIndex(index="a").source, None)


class FakeLogES:
    def __init__(self):
        self.searched = []

    def req(self, method, path, body=None, ok=None):
        self.searched.append((path.split("/")[1], body["fields"]))
        ix = path.split("/")[1]
        if ix == "k8s-*":
            hit = {"fields": {"@timestamp": ["2026-11-07T03:00:01Z"], "resource.attributes.service.name": ["wip-worker"], "log.level": ["error"], "trace_id": ["t1"], "message": ["otel log"]}}
        else:
            hit = {"fields": {"@timestamp": ["2026-11-07T03:00:02Z"], "service.name": ["mes-api"], "log.level": ["ERROR"], "trace.id": ["t1"], "message": ["apm log"]}}
        return {"hits": {"hits": [hit]}}

    def search(self, index, body):
        return self.req("POST", f"/{index}/_search", body)


class Logs(unittest.TestCase):
    def test_each_source_queried_with_its_own_index_and_field_names(self):
        es = FakeLogES()
        with env(AIOPS_SOURCE="otel,apm"):
            repo = LogRepository(es, load_scenario())
        got = repo.of_traces("k8s-*", ["t1"], T0, T0 + timedelta(minutes=1))
        self.assertEqual([r["message"] for r in got], ["otel log", "apm log"])             # 依時間排；兩個來源都有
        self.assertEqual([r["service"] for r in got], ["wip-worker", "mes-api"])           # 各用各的欄位名取出來
        self.assertEqual([i for i, _ in es.searched], ["k8s-*", "logs-apm.app.*,logs-apm.error-*"])

    def test_single_source_queries_only_the_given_index(self):
        es = FakeLogES()
        with env(AIOPS_SOURCE="otel"):
            repo = LogRepository(es, load_scenario())
        repo.of_traces("k8s-*", ["t1"], T0, T0 + timedelta(minutes=1))
        self.assertEqual([i for i, _ in es.searched], ["k8s-*"])


if __name__ == "__main__":
    unittest.main()

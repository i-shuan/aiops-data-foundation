"""trace 合成一筆（detector/trace_merge.py）：文件長什麼樣、入口怎麼挑、index 依 group 與日期、從合成的讀回來跟原本一樣。
不連 ES（docs/pdca/phase1-collect/2026-10-trace-merge.md）。"""
import unittest
from datetime import datetime, timezone

from shared.infra.repos import Repos
from shared.scenario import load_scenario

SC = load_scenario()
import detect.store.trace as T
import detect.trace.span_merge as M

T0 = 1_793_400_000_000.0   # 2026-10-31T00:00:00Z 附近（毫秒）


def span(sid, parent, service, name, kind, start, dur, error=False, target=None, group="it", attrs=None):
    return {"trace_id": "t1", "span_id": sid, "parent": parent, "service": service, "name": name, "kind": kind, "target": target,
            "start_ms": T0 + start, "dur_ms": dur, "error": error, "exception": None, "links": [],
            "attrs": attrs or {}, "resource": {"service.name": service, "group": group, "dept": "D3120", "k8s.pod.name": f"{service}-0"}}


def a_trace():
    return [span("a", None, "mes-api", "GET /lots/{id}", "Server", 0, 300),
            span("b", "a", "mes-api", "GET → wip-worker", "Client", 10, 250, target="wip-worker"),
            span("c", "b", "wip-worker", "GET /wip", "Server", 20, 200),
            span("d", "c", "wip-worker", "SELECT LOT", "Client", 30, 150, error=True, target="oracle")]


class BuildTest(unittest.TestCase):
    def test_summary_and_index(self):
        index, doc = T.merged_doc(a_trace(), datetime(2026, 10, 31, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(index, f"traces-merged-it-{datetime.fromtimestamp(T0 / 1000, timezone.utc):%Y.%m.%d}")
        self.assertEqual(doc["trace_id"], "t1")
        self.assertEqual((doc["group"], doc["dept"]), ("it", "D3120"))
        self.assertEqual(doc["root"], {"service": "mes-api", "name": "GET /lots/{id}", "kind": "Server", "error": False})
        self.assertEqual(doc["duration_ms"], 300)
        self.assertTrue(doc["error"])                       # 有一段錯就算錯
        self.assertEqual(doc["services"], ["mes-api", "wip-worker"])
        self.assertEqual(doc["span_count"], 4)
        self.assertEqual([s["span_id"] for s in doc["spans"]], ["a", "b", "c", "d"])   # 依開始時間排

    def test_entries_are_server_consumer_and_parentless(self):
        spans = a_trace() + [span("e", None, "worker", "SELECT X", "Client", 40, 5, target="pg"),   # 沒有上一層：也算入口
                             span("f", "a", "mes-api", "consume", "Consumer", 50, 5)]
        _, doc = T.merged_doc(spans, datetime.now(timezone.utc))
        self.assertEqual(sorted(e["span_id"] for e in doc["entries"]), ["a", "c", "e", "f"])
        e = next(x for x in doc["entries"] if x["span_id"] == "c")
        self.assertEqual((e["service"], e["name"], e["kind"], e["duration_ms"], e["error"]), ("wip-worker", "GET /wip", "Server", 200, False))

    def test_duration_covers_children_that_end_later(self):
        spans = [span("a", None, "s", "GET /x", "Server", 0, 100), span("b", "a", "s", "bg", "Internal", 50, 200)]
        _, doc = T.merged_doc(spans, datetime.now(timezone.utc))
        self.assertEqual(doc["duration_ms"], 250)

    def test_group_falls_back_to_unassigned(self):
        s = span("a", None, "s", "GET /x", "Server", 0, 1)
        s["resource"] = {"service.name": "s"}
        index, doc = T.merged_doc([s], datetime.now(timezone.utc))
        self.assertTrue(index.startswith("traces-merged-unassigned-"))
        self.assertIsNone(doc["dept"])


class RoundTripTest(unittest.TestCase):
    def test_from_merged_gives_back_the_same_spans(self):
        spans = a_trace()
        _, doc = T.merged_doc(spans, datetime.now(timezone.utc))
        back = T.from_merged(doc)
        self.assertEqual(len(back), 4)
        for orig, got in zip(sorted(spans, key=lambda s: s["start_ms"]), back):
            for k in ("trace_id", "span_id", "parent", "service", "name", "kind", "target", "start_ms", "dur_ms", "error", "exception", "links", "attrs"):
                self.assertEqual(got[k], orig[k], k)
        # 組回來的樹算得出跟原本一樣的東西
        self.assertEqual(T.root_of(back)["span_id"], "a")
        self.assertEqual([s["span_id"] for s in T.error_origins(back)], ["d"])
        st = T.self_times(back)
        self.assertAlmostEqual(st["a"], 50)      # 300 − 子 span 250


class ReadersTest(unittest.TestCase):
    """讀端用 merged 的 index：用假的 ES 看查詢長什麼樣、結果怎麼轉。"""

    class FakeES:
        def __init__(self, hits):
            self.hits, self.bodies = hits, []

        def search(self, index, body):
            self.bodies.append((index, body))
            return {"hits": {"hits": self.hits, "total": {"value": len(self.hits)}}, "aggregations": {}}

    def test_spans_of_merged_index_expands_documents(self):
        _, doc = T.merged_doc(a_trace(), datetime.now(timezone.utc))
        es = self.FakeES([{"_id": "t1", "_source": doc}])
        spans = Repos(es, SC).traces.spans("traces-merged-*", ["t1"], datetime(2026, 10, 30, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(len(spans), 4)
        index, body = es.bodies[0]
        self.assertEqual(index, "traces-merged-*")
        self.assertIn({"terms": {"trace_id": ["t1"]}}, body["query"]["bool"]["filter"])

    def test_spans_by_id_uses_nested_query(self):
        _, doc = T.merged_doc(a_trace(), datetime.now(timezone.utc))
        es = self.FakeES([{"_id": "t1", "_source": doc}])
        got = Repos(es, SC).traces.spans_by_id("traces-merged-*", ["c"], datetime(2026, 10, 30, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(list(got), ["c"])
        self.assertEqual(got["c"]["service"], "wip-worker")
        body = es.bodies[0][1]
        self.assertEqual(body["query"]["bool"]["filter"][1]["nested"]["path"], "spans")


class MalformedSpanTest(unittest.TestCase):
    def test_bad_duration_does_not_block_merge(self):
        """壞 SDK 送來 duration 是字串、負數：當 0，不讓這一筆擋住整批（ES 允許格式錯誤的值進來）。"""
        base = {"@timestamp": "2026-10-31T16:13:00.000+00:00", "trace_id": "t", "span_id": "s", "name": "GET /bad"}
        self.assertEqual(T.to_span({**base, "duration": "not-a-number"})["dur_ms"], 0.0)
        self.assertEqual(T.to_span({**base, "duration": -5})["dur_ms"], 0.0)
        self.assertEqual(T.to_span({**base})["dur_ms"], 0.0)
        self.assertEqual(T.to_span({**base, "duration": 20_000_000})["dur_ms"], 20.0)
        self.assertEqual(T.to_span({**base, "duration": "20000000"})["dur_ms"], 20.0)


if __name__ == "__main__":
    unittest.main()

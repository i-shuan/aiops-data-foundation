"""ES 也是 Backend（docs/pdca/meta/2026-11-oop-core.md）：open_backend 永遠回 Backend、as_backend 是唯一的正規化點、
engine_version 每種 Store 都有、程式裡不再有 is_backend／hasattr(es, "es") 的分支。不連 ES。"""
import os
import unittest

import pandas as pd

import sources
from core.version import ENGINE_VERSION
from detector import owner as O
from detector import rules as R
from detector import es as DE
from investigator import similar as SIM
from sources.base import as_backend

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)


class FakeES:
    """只有 req／search／bulk_index 的假 ES（跟其他測試裡的一樣）：回空結果、記下寫了什麼。"""
    def __init__(self, owners=None, hits=None):
        self.owners, self.hits, self.written, self.calls = owners or {}, hits or [], [], []

    def req(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if "_search" in path and "metrics" in path:   # owner.load_owners
            return {"aggregations": {"ns": {"buckets": []}, "svc": {"buckets": [
                {"key": k, "d": {"buckets": [{"key": d}]}, "g": {"buckets": [{"key": g}]}} for k, (d, g) in self.owners.items()]}}}
        return {"hits": {"hits": self.hits}}

    def search(self, index, body):
        self.calls.append(("search", index, body))
        return {"hits": {"hits": self.hits}}

    def bulk_index(self, index, docs):
        self.written.append((index, docs))


class OpenBackend(unittest.TestCase):
    def test_no_source_is_es_backend_wrapping_the_factory_object(self):
        es = FakeES()
        be = sources.open_backend({"series": []}, lambda: es)
        self.assertIsInstance(be, sources.ESBackend)
        self.assertIs(be.es, es)
        self.assertEqual((be.kind, be.store.kind), ("es", "es"))
        self.assertIs(be.store.es, es)


class AsBackend(unittest.TestCase):
    def test_wraps_fake_es_and_keeps_backends(self):
        es = FakeES()
        be = as_backend(es)
        self.assertIsInstance(be, sources.ESBackend)
        self.assertIs(be.es, es)
        mem = sources.MemoryBackend({})
        self.assertIs(as_backend(mem), mem)
        with self.assertRaises(TypeError):
            as_backend(None)
        with self.assertRaises(TypeError):
            as_backend("not an es")

    def test_raw_calls_only_on_es(self):
        self.assertEqual(as_backend(FakeES()).raw_search("x-*", {"size": 1}), {"hits": {"hits": []}})
        with self.assertRaises(NotImplementedError):
            sources.MemoryBackend({}).raw_search("x-*", {})
        with self.assertRaises(NotImplementedError):
            sources.MemoryBackend({}).raw_req("PUT", "/_index_template/x", {})
        # Store 層的 req 還是「不是 ES 當作沒事」（cmd_rules 等補 mapping 用）
        self.assertEqual(sources.MemoryBackend({}).req("PUT", "/aiops-anomalies/_mapping", {}), {})


class WriteAnomalies(unittest.TestCase):
    def test_memory_store_gets_engine_version_and_unassigned(self):
        be = sources.MemoryBackend({})
        O.write_anomalies(be, [("a", {"keys": {"service": "mes-api"}})])
        (_, doc), = be.store.docs("aiops-anomalies")
        self.assertEqual((doc["engine_version"], doc["dept"], doc["group"]), (ENGINE_VERSION, None, "unassigned"))

    def test_es_store_gets_owner_and_engine_version(self):
        es = FakeES(owners={"mes-api": ("D3120", "it")})
        O.write_anomalies(sources.ESBackend(es), [("a", {"keys": {"service": "mes-api"}}), ("b", {"keys": {"service": "nobody"}})])
        (index, docs), = es.written
        self.assertEqual(index, "aiops-anomalies")
        self.assertEqual([(d["dept"], d["group"], d["engine_version"]) for _, d in docs],
                         [("D3120", "it", ENGINE_VERSION), (None, "unassigned", ENGINE_VERSION)])


class EsBodiesMoved(unittest.TestCase):
    """搬進 ESSourceBackend／ESStore 的查詢，透過 detector/es.py、rules.py、similar.py 的函式名呼叫，打到的還是同一種 body。"""
    T0 = pd.Timestamp("2026-10-05T00:00:00Z")

    def test_series_renames_by_to_keys_and_pages_composite(self):
        es = FakeES()
        es.req = lambda m, p, b=None, **k: {"aggregations": {"s": {"buckets": [
            {"key": {"k0": "mes-api", "ts": int(self.T0.value // 10**6)}, "doc_count": 3}]}}}
        df = DE.fetch(es, {"name": "n", "index": "m-*", "by": ["service.name"], "keys": ["service"], "value": "count"},
                      self.T0, self.T0 + pd.Timedelta(hours=1), "1h")
        self.assertEqual(list(df.columns), ["service", "ts", "value", "n"])
        self.assertEqual(df["value"].tolist(), [3.0])

    def test_points_join_fills_denominator(self):
        es = FakeES()
        rows = {"metrics.used": [{"@timestamp": self.T0.isoformat(), "pod": "p", "metrics": {"used": 8.0}}],
                "metrics.max": [{"@timestamp": self.T0.isoformat(), "pod": "p", "metrics": {"max": 10.0}}]}
        es.search = lambda index, body: {"hits": {"hits": [{"_source": d} for d in rows[body["_source"][1]]]}}
        got = R.fetch(es, {"index": "k8s-*", "field": "metrics.used", "by": ["pod"], "type": "ratio", "den": ["metrics.max"],
                           "join": {"field": "metrics.max"}}, self.T0, self.T0 + pd.Timedelta(minutes=1))
        self.assertEqual(got[("p",)][0][1]["metrics"], {"used": 8.0, "max": 10.0})

    def test_similar_search_uses_query_and_confirmed_cases_require_fields(self):
        es = FakeES(hits=[{"_id": "i1", "_source": {"status": "confirmed", "fingerprint": {"services": ["mes-api"]}, "root_cause_category": "db"}}])
        fp = {"services": ["mes-api"], "routes": [], "series": [], "slow_segments": [], "error_origins": [], "patterns": []}
        self.assertEqual([g["incident_id"] for g in SIM.search(es, fp, size=7)], [])   # 只有同服務 3 分，不夠像
        _, index, body = es.calls[-1]
        self.assertEqual((index, body["size"]), ("aiops-incidents", 7))
        self.assertEqual(body["query"]["bool"]["filter"][0], {"terms": {"status": list(SIM.CONFIRMED)}})
        self.assertEqual(SIM._confirmed_cases(es)[0][0], "i1")
        _, _, body = es.calls[-1]
        self.assertIn({"exists": {"field": "fingerprint"}}, body["query"]["bool"]["filter"])


class NoTypeBranches(unittest.TestCase):
    def test_no_is_backend_or_hasattr_es_left(self):
        """重構的重點：程式裡不再有「傳進來的是不是 Backend」的分支。"""
        bad = []
        for pkg in ("detector", "investigator", "core", "plugins", "sources", "api"):
            for root, _, files in os.walk(os.path.join(CODE, pkg)):
                for fn in files:
                    if fn.endswith(".py"):
                        path = os.path.join(root, fn)
                        with open(path, encoding="utf-8") as f:
                            text = f.read()
                        if "is_backend(" in text or 'hasattr(es, "es")' in text:
                            bad.append(os.path.relpath(path, CODE))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()

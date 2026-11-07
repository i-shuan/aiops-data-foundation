"""異常總表的歸屬（detect/store/owner_repo.py）與寫入（AnomalyRepository.write）：依 namespace、服務對回 dept、group。"""
import unittest

import data.store.owner_repo as O
from common.infra.repos import Repos
from common.scenario import load_scenario

IT = load_scenario("observability")


def bucket(key, dept, group):
    return {"key": key, "d": {"buckets": [{"key": dept}] if dept else []}, "g": {"buckets": [{"key": group}] if group else []}}


class OwnersFromAgg(unittest.TestCase):
    def test_parse(self):
        res = {"aggregations": {"ns": {"buckets": [bucket("mes-prod", "D3120", "it"), bucket("kube-system", None, None)]},
                                "svc": {"buckets": [bucket("mes-api", "D3120", "it")]}}}
        o = O.owners_from_agg(res)
        self.assertEqual(o[("ns", "mes-prod")], ("D3120", "it"))
        self.assertEqual(o[("ns", "kube-system")], (None, None))
        self.assertEqual(o[("svc", "mes-api")], ("D3120", "it"))

    def test_empty(self):
        self.assertEqual(O.owners_from_agg({}), {})


class OwnerOf(unittest.TestCase):
    owners = {("ns", "mes-prod"): ("D3120", "it"), ("ns", "wip-prod"): ("D1110", "imc"),
              ("svc", "mes-api"): ("D3120", "it"), ("svc", "kubelet"): (None, None)}

    def test_namespace_first(self):
        # namespace 跟服務對到不同部門時以 namespace 為準：dept 是 namespace 的 label
        keys = {"resource.attributes.k8s.namespace.name": "wip-prod", "resource.attributes.service.name": "mes-api"}
        self.assertEqual(O.owner_of(keys, self.owners, IT.entity_keys), ("D1110", "imc"))

    def test_service_when_no_namespace(self):
        self.assertEqual(O.owner_of({"service": "mes-api", "method": "GET"}, self.owners, IT.entity_keys), ("D3120", "it"))
        self.assertEqual(O.owner_of({"entry.service": "mes-api"}, self.owners, IT.entity_keys), ("D3120", "it"))
        self.assertEqual(O.owner_of({"caller": "mes-api", "callee": "oracle"}, self.owners, IT.entity_keys), ("D3120", "it"))

    def test_log_and_peer_key_names(self):
        self.assertEqual(O.owner_of({"namespace": "mes-prod", "service": "mes-api", "pod": "p-1"}, self.owners, IT.entity_keys), ("D3120", "it"))

    def test_unknown_is_unassigned(self):
        # 節點的規則（只有 node）、查不到的服務：跟 Collector 對沒有 dept 的處理一樣
        self.assertEqual(O.owner_of({"resource.attributes.k8s.node.name": "n1"}, self.owners, IT.entity_keys), (None, "unassigned"))
        self.assertEqual(O.owner_of({"service": "new-svc"}, self.owners, IT.entity_keys), (None, "unassigned"))
        self.assertEqual(O.owner_of({"service": "kubelet"}, self.owners, IT.entity_keys), (None, "unassigned"))   # 有紀錄但沒 dept
        self.assertEqual(O.owner_of({}, self.owners, IT.entity_keys), (None, "unassigned"))
        self.assertEqual(O.owner_of(None, {}, IT.entity_keys), (None, "unassigned"))

    def test_group_missing_but_dept_known(self):
        self.assertEqual(O.owner_of({"namespace": "x"}, {("ns", "x"): ("D9999", None)}, IT.entity_keys), ("D9999", "unassigned"))


class WriteAnomalies(unittest.TestCase):
    def test_adds_owner_and_writes(self):
        class Fake:
            def __init__(self): self.written = None
            def req(self, *a, **k):
                return {"aggregations": {"ns": {"buckets": [bucket("mes-prod", "D3120", "it")]}, "svc": {"buckets": []}}}
            def bulk_index(self, index, docs): self.written = (index, docs)
        es = Fake()
        docs = [("a", {"keys": {"namespace": "mes-prod"}}), ("b", {"keys": {"node": "n1"}})]
        Repos(es, IT).anomalies.write(docs)
        self.assertEqual(es.written[0], "aiops-anomalies")
        self.assertEqual([(d["dept"], d["group"]) for _, d in es.written[1]], [("D3120", "it"), (None, "unassigned")])

    def test_every_doc_carries_engine_version(self):
        # 五種寫入（baseline、規則、peer、quality、trace）都走這裡，所以在這裡補就等於每筆都有；欄位也要在 mapping（dynamic false）
        from common.infra.env import ENGINE_VERSION
        from detect.store.models import ANOMALY_MAPPING
        class Fake:
            def __init__(self): self.written = None
            def req(self, *a, **k): return {}
            def bulk_index(self, index, docs): self.written = docs
        es = Fake()
        Repos(es, IT).anomalies.write([("a", {"keys": {"service": "x"}}), ("b", {"keys": {}})])
        self.assertEqual([d["engine_version"] for _, d in es.written], [ENGINE_VERSION, ENGINE_VERSION])
        self.assertEqual(ANOMALY_MAPPING["mappings"]["properties"]["engine_version"], {"type": "keyword"})

    def test_empty_does_not_query(self):
        class Boom:
            def req(self, *a, **k): raise AssertionError("不該查")
        Repos(Boom(), IT).anomalies.write([])


if __name__ == "__main__":
    unittest.main()

"""欄位對照的覆寫擴到 trace（docs/pdca/meta/2026-11-mapping-trace.md）。為了確認：
1. trace 合成時的部門、群組用歸屬的 dept、group 欄位名（含核准過的覆寫），跟 metrics、log 的異常讀同一個欄位；
   覆寫的欄位不在預設的 _source 清單裡也要讀得到。
2. raw 索引的覆寫：只換那個來源的索引，其他來源照 trace.yaml；trace.yaml 沒列的來源加上去；沒覆寫就原樣。
3. 試 raw 索引：抽到的文件要是那個來源的格式；沒資料是 nodata（核准會被擋）。
4. 清單與提案算得出 trace 的 yaml 值（從排程表找 trace-merge 用的 trace.yaml）。"""
import copy
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

import common.scenario.mapping as MP
import data.mapping_service as M
from common.scenario import load_scenario
from data.source.adapter import apply_owner, with_paths
from data.store.models import Trace
from data.store.trace_repo import TraceRepository
from interface.config.schedule import trace_raw_pairs
from tests.test_apm import TX
from tests.test_mapping import FakeES


def scenario(source="apm"):
    with mock.patch.dict(os.environ, {"AIOPS_PLUGIN": "observability", "AIOPS_SOURCE": source}):
        return load_scenario()


def active(source, section, field, value):
    return {"source": source, "section": section, "field": field, "value": value, "status": "active", "approved_at": "1"}


class TraceEs(FakeES):
    """raw trace 的搜尋回 sample，並記下要的 _source。"""
    def req(self, method, path, body=None, ok=None):
        if path.split("?")[0].strip("/").split("/")[0] != MP.MAPPING_TABLE and body and "_source" in body:
            self.asked = body["_source"]
        return super().req(method, path, body, ok)


class OwnerFields(unittest.TestCase):
    def setUp(self):
        MP._cache.clear()

    def test_apply_owner_reads_the_owner_paths_and_drops_missing(self):
        doc = {"labels": {"team": "mes", "dept": "D3120"}}
        span = {"resource": {"dept": "D3120", "group": "it"}}
        out = apply_owner(copy.deepcopy(span), doc, {"dept": "labels.dept", "group": "labels.team"})
        self.assertEqual(out["resource"], {"dept": "D3120", "group": "mes"})
        out = apply_owner(copy.deepcopy(span), doc, {"dept": "labels.nope", "group": "labels.team"})
        self.assertEqual(out["resource"], {"group": "mes"})                     # 文件沒有那個欄位：拿掉（跟沒設 label 一樣）
        self.assertEqual(apply_owner(copy.deepcopy(span), doc, {})["resource"], span["resource"])   # 歸屬沒寫：不動

    def test_with_paths_adds_to_list_and_includes(self):
        self.assertEqual(with_paths(["a", "b"], ["b", "c"]), ["a", "b", "c"])
        self.assertEqual(with_paths({"includes": ["a"], "excludes": ["x"]}, ["c"]), {"includes": ["a", "c"], "excludes": ["x"]})
        self.assertEqual(with_paths(["a"], []), ["a"])

    def test_merge_uses_the_overridden_group_and_dept(self):
        doc = copy.deepcopy(TX)
        doc["labels"]["app"] = "mes"
        doc["custom"] = {"dept": "D9999"}
        es = TraceEs(sample=[doc])
        repo = TraceRepository(es, scenario(), db=es.db)
        start, end = datetime(2026, 11, 2, tzinfo=timezone.utc), datetime(2026, 11, 3, tzinfo=timezone.utc)
        spans = repo.spans("traces-apm-*", ["t1"], start, end)
        index, merged = Trace.group(spans)["t1"].to_merged_doc(end)
        self.assertEqual((merged["dept"], merged["group"]), ("D3120", "it"))
        es.docs = {"g": active("apm", "owner", "group", "labels.app"), "d": active("apm", "owner", "dept", "custom.dept")}
        MP._cache.clear()
        spans = repo.spans("traces-apm-*", ["t1"], start, end)
        index, merged = Trace.group(spans)["t1"].to_merged_doc(end)
        self.assertEqual((merged["dept"], merged["group"]), ("D9999", "mes"))
        self.assertIn("mes", index)                                              # traces-merged-<group>-… 也跟著
        self.assertIn("custom.dept", es.asked["includes"])                       # 不在預設清單的欄位要多要

    def test_otel_yaml_paths_match_what_the_adapter_already_reads(self):
        doc = {"@timestamp": "2026-11-02T01:00:00Z", "trace_id": "t1", "span_id": "s1", "name": "GET /", "kind": "Server", "duration": 1000000,
               "resource": {"attributes": {"service.name": "a", "dept": "D1", "group": "g1"}}}
        es = TraceEs(sample=[doc])
        repo = TraceRepository(es, scenario("otel"), db=es.db)
        start, end = datetime(2026, 11, 2, tzinfo=timezone.utc), datetime(2026, 11, 3, tzinfo=timezone.utc)
        res = repo.spans("traces-generic.otel-*", ["t1"], start, end)[0]["resource"]
        self.assertEqual((res["dept"], res["group"]), ("D1", "g1"))


class RawIndex(unittest.TestCase):
    def setUp(self):
        MP._cache.clear()

    def eff(self, source, *overrides):
        es = FakeES()
        es.docs = {str(i): o for i, o in enumerate(overrides)}
        return MP.effective(scenario(source), es.db)

    def test_no_override_is_unchanged(self):
        pairs = [("traces-apm-*", None)]
        self.assertEqual(M.raw_indexes(self.eff("apm"), pairs), pairs)

    def test_override_replaces_only_that_source(self):
        sc = self.eff("otel,apm", active("apm", "trace", "raw_index", "traces-apm.new-*"))
        pairs = [("traces-generic.otel-*", "otel"), ("traces-apm-*", "apm")]
        self.assertEqual(M.raw_indexes(sc, pairs), [("traces-generic.otel-*", "otel"), ("traces-apm.new-*", "apm")])
        self.assertEqual(M.raw_indexes(sc, [("traces-generic.otel-*", "otel")]),        # trace.yaml 沒列 apm：加上去
                         [("traces-generic.otel-*", "otel"), ("traces-apm.new-*", "apm")])

    def test_unspecified_source_is_resolved_by_index_name(self):
        sc = self.eff("apm", active("apm", "trace", "raw_index", "traces-apm.new-*"))
        self.assertEqual(M.raw_indexes(sc, [("traces-apm-*", None)]), [("traces-apm.new-*", "apm")])

    def test_try_checks_the_format_of_the_source(self):
        sc = scenario()
        self.assertEqual(M.try_value(FakeES(sample=[TX]), sc, "apm", "trace", "raw_index", "traces-apm-*"), [])
        otel = {"trace_id": "t1", "span_id": "s1"}
        got = M.try_value(FakeES(sample=[otel]), sc, "apm", "trace", "raw_index", "traces-generic.otel-*")
        self.assertTrue(got and got[0].counts and "otel" in got[0].problem)
        got = M.try_value(FakeES(sample=[]), sc, "apm", "trace", "raw_index", "traces-nothing-*")
        self.assertEqual(got[0].kind, "nodata")

    def test_cannot_approve_an_index_with_the_wrong_format(self):
        sc = scenario()
        es = FakeES(sample=[{"trace_id": "t1", "span_id": "s1"}])
        _id, _ = M.propose(es, es.db, sc, "apm", "trace", "raw_index", "traces-generic.otel-*", "王小明", pairs=[("traces-apm-*", None)])
        ok, msg = M.approve(es, es.db, sc, _id, "李主管")
        self.assertFalse(ok, msg)
        self.assertEqual(M.raw_indexes(MP.effective(sc, es.db), [("traces-apm-*", None)]), [("traces-apm-*", None)])

    def test_current_and_propose_know_the_yaml_value(self):
        pairs = trace_raw_pairs("configs/apm/schedule.yaml")
        self.assertEqual(pairs, [("traces-apm-*", None)])
        sc, es = scenario(), FakeES(sample=[TX])
        row = next(r for r in M.current(es.db, sc, pairs) if r["section"] == "trace")
        self.assertEqual((row["yaml"], row["value"], row["override"]), ("traces-apm-*", "traces-apm-*", None))
        with self.assertRaises(ValueError):                                        # 跟現在一樣
            M.propose(es, es.db, sc, "apm", "trace", "raw_index", "traces-apm-*", "王小明", pairs=pairs)
        _id, _ = M.propose(es, es.db, sc, "apm", "trace", "raw_index", "traces-apm.new-*", "王小明", pairs=pairs)
        self.assertEqual(es.docs[_id]["old_value"], "traces-apm-*")
        ok, msg = M.approve(es, es.db, sc, _id, "李主管")
        self.assertTrue(ok, msg)
        row = next(r for r in M.current(es.db, sc, pairs) if r["section"] == "trace")
        self.assertEqual((row["yaml"], row["value"], row["override"]), ("traces-apm-*", "traces-apm.new-*", _id))

    def test_schedule_without_trace_merge_is_none(self):
        self.assertIsNone(trace_raw_pairs("configs/factory/schedule.yaml"))


if __name__ == "__main__":
    unittest.main()

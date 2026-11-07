"""欄位對照的覆寫（aiops-mapping）：提、試、核准、退回、停用，蓋在情境資料檔上。為了確認：
1. 只開放歸屬、log 的欄位與 trace 的 raw 索引；值寫錯在提的時候就擋。
2. 只有核准（active）的才生效；proposed、rejected、disabled 都不影響。
3. 核准前一定對真的資料試：新的欄位名一次都沒出現就不給核准（--force 才行）；同一個欄位新的核准會把舊的停用。
4. 覆寫只改這一輪用的情境，不動 yaml；讀不到 ES 照 yaml 跑；壞掉的一筆略過不擋其他筆。
5. 並存（otel,apm）時改得到次要來源那一份。
6. 網頁：看得到現在生效的、提得出、先試不存；核准不在網頁。"""
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import data.mapping_service as M
import common.scenario.mapping as MP
from common.scenario import load_scenario

APM_METRIC = {"@timestamp": "2026-11-07T03:00:00Z", "service": {"name": "mes-api", "node": {"name": "mes-api-0"}},
              "labels": {"dept": "D3120", "group": "mes", "app": "mes"}, "kubernetes": {"namespace": "mes"}}


class FakeES:
    """aiops-mapping 存在記憶體；其他索引的搜尋回 sample（抽樣用）。"""
    def __init__(self, sample=None, down=False):
        self.docs, self.sample, self.down = {}, sample if sample is not None else [APM_METRIC], down

    def req(self, method, path, body=None, ok=None):
        if self.down:
            raise ConnectionError("es down")
        parts = path.split("?")[0].strip("/").split("/")
        if parts[0] == MP.MAPPING_INDEX:
            if parts[-1] == "_search":
                return {"hits": {"hits": [{"_id": k, "_source": v} for k, v in self.docs.items()]}}
            if parts[1] == "_doc" and method == "GET":
                return {"found": parts[2] in self.docs, "_source": self.docs.get(parts[2])}
            if parts[1] == "_doc" and method == "PUT":
                self.docs[parts[2]] = dict(body)
                return {}
            if parts[1] == "_update":
                self.docs[parts[2]].update(body["doc"])
                return {}
        if parts[-1] == "_search":
            return {"hits": {"hits": [{"_id": str(i), "_source": d} for i, d in enumerate(self.sample)]}}
        return {}

    def ensure_index(self, name, mapping):
        pass

    def search(self, index, body):
        return self.req("POST", f"/{index}/_search", body)


def apm():
    with mock.patch.dict(os.environ, {"AIOPS_PLUGIN": "observability", "AIOPS_SOURCE": "apm"}):
        return load_scenario()


class Validate(unittest.TestCase):
    def test_only_owner_and_log_fields(self):
        self.assertEqual(MP.check_value("owner", "service", " labels.app "), "labels.app")
        self.assertEqual(MP.check_value("log", "message", "message, error.exception.message"), ["message", "error.exception.message"])
        for bad in (("owner", "nope", "x"), ("series", "index", "x"), ("owner", "service", ""), ("log", "message", " , ")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                MP.check_value(*bad)


class Apply(unittest.TestCase):
    def setUp(self):
        MP._cache.clear()

    def test_only_active_overrides_take_effect_and_yaml_is_untouched(self):
        sc = apm()
        es = FakeES()
        es.docs = {"a": {"source": "apm", "section": "owner", "field": "dept", "value": "labels.group", "status": "active", "approved_at": "1"},
                   "p": {"source": "apm", "section": "owner", "field": "service", "value": "labels.app", "status": "proposed"},
                   "d": {"source": "apm", "section": "log", "field": "level", "value": "x", "status": "disabled"}}
        eff = MP.effective(sc, es)
        self.assertEqual((eff.owner["dept"], eff.owner["service"], eff.log["level"]), ("labels.group", "service.name", "log.level"))
        self.assertEqual(sc.owner["dept"], "labels.dept")                       # 原本的情境（yaml）沒被改

    def test_es_down_means_yaml(self):
        sc = apm()
        self.assertIs(MP.effective(sc, FakeES(down=True)), sc)

    def test_a_broken_override_is_skipped_not_fatal(self):
        sc = apm()
        got = MP.apply(sc, [{"id": "bad", "source": "apm", "section": "owner", "field": "nope", "value": "x"},
                            {"id": "ok", "source": "apm", "section": "owner", "field": "group", "value": "labels.app"}])
        self.assertEqual(got.owner["group"], "labels.app")

    def test_mixed_sources_can_override_the_secondary_profile(self):
        with mock.patch.dict(os.environ, {"AIOPS_PLUGIN": "observability", "AIOPS_SOURCE": "otel,apm"}):
            sc = load_scenario()
        got = MP.apply(sc, [{"source": "apm", "section": "log", "field": "trace_id", "value": "labels.trace"}])
        self.assertEqual((got.log["trace_id"], got.extra_logs[0]["trace_id"]), ("trace_id", "labels.trace"))


class Flow(unittest.TestCase):
    def setUp(self):
        MP._cache.clear()
        self.es, self.sc = FakeES(), apm()

    def test_propose_does_not_take_effect_until_approved(self):
        _id, found = M.propose(self.es, self.sc, "apm", "owner", "dept", "labels.group", "王小明", "部門改用 group")
        self.assertEqual(found, [])                                              # 試過：labels.group 在資料裡有
        self.assertEqual(self.es.docs[_id]["status"], "proposed")
        self.assertEqual(MP.effective(self.sc, self.es).owner["dept"], "labels.dept")
        ok, msg = M.approve(self.es, self.sc, _id, "李主管")
        self.assertTrue(ok, msg)
        self.assertEqual(MP.effective(self.sc, self.es).owner["dept"], "labels.group")
        row = next(r for r in M.current(self.es, self.sc) if (r["section"], r["field"]) == ("owner", "dept"))
        self.assertEqual((row["yaml"], row["value"], row["override"]), ("labels.dept", "labels.group", _id))

    def test_a_field_that_is_not_in_the_data_cannot_be_approved_without_force(self):
        _id, found = M.propose(self.es, self.sc, "apm", "owner", "service", "labels.nope", "王小明")
        self.assertTrue(found and found[0].counts)                               # 提的時候就看得到「一次都沒出現」
        ok, msg = M.approve(self.es, self.sc, _id, "李主管")
        self.assertFalse(ok)
        self.assertIn("--force", msg)
        self.assertEqual(MP.effective(self.sc, self.es).owner["service"], "service.name")
        ok, _ = M.approve(self.es, self.sc, _id, "李主管", force=True)
        self.assertTrue(ok)

    def test_new_approval_disables_the_old_one_and_disable_goes_back_to_yaml(self):
        a, _ = M.propose(self.es, self.sc, "apm", "owner", "dept", "labels.group", "甲")
        M.approve(self.es, self.sc, a, "主管")
        b, _ = M.propose(self.es, self.sc, "apm", "owner", "dept", "labels.app", "乙")
        M.approve(self.es, self.sc, b, "主管")
        self.assertEqual((self.es.docs[a]["status"], self.es.docs[b]["status"]), ("disabled", "active"))
        self.assertEqual(MP.effective(self.sc, self.es).owner["dept"], "labels.app")
        ok, _ = M.set_status(self.es, b, "disabled", "主管")
        self.assertTrue(ok)
        self.assertEqual(MP.effective(self.sc, self.es).owner["dept"], "labels.dept")

    def test_reject_and_wrong_transitions(self):
        _id, _ = M.propose(self.es, self.sc, "apm", "log", "level", "labels.app", "甲")
        self.assertFalse(M.set_status(self.es, _id, "disabled", "主管")[0])      # proposed 不能 disable
        self.assertTrue(M.set_status(self.es, _id, "rejected", "主管")[0])
        self.assertFalse(M.approve(self.es, self.sc, _id, "主管")[0])             # rejected 不能再核准

    def test_propose_guards(self):
        with self.assertRaises(ValueError):
            M.propose(self.es, self.sc, "apm", "owner", "dept", "labels.group", "")          # 沒寫是誰
        with self.assertRaises(ValueError):
            M.propose(self.es, self.sc, "apm", "owner", "dept", "labels.dept", "甲")          # 跟現在一樣
        with self.assertRaises(ValueError):
            M.propose(self.es, self.sc, "otel", "owner", "dept", "x", "甲")                   # 沒在用的來源


class Page(unittest.TestCase):
    def setUp(self):
        MP._cache.clear()
        import interface.web.routes as R
        self.R, self.es = R, FakeES()
        self.client = TestClient(R.app)
        R.state.es, R.state.repo = self.es, type("Repo", (), {"es": self.es, "scenario": apm()})()

    def test_page_lists_current_values_and_proposes(self):
        page = self.client.get("/mapping").text
        self.assertIn("labels.dept", page)
        self.assertIn("approve", page)                                           # 告訴人核准在命令列
        r = self.client.post("/mapping", data={"source": "apm", "key": "owner.dept", "value": "labels.group", "by": "王小明", "action": "try"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("試過", r.text)
        self.assertEqual(self.es.docs, {})                                       # 試不存
        r = self.client.post("/mapping", data={"source": "apm", "key": "owner.dept", "value": "labels.group", "by": "王小明", "action": "propose"})
        self.assertIn("已提", r.text)
        self.assertEqual([d["status"] for d in self.es.docs.values()], ["proposed"])

    def test_bad_input_is_a_400_with_the_reason(self):
        r = self.client.post("/mapping", data={"source": "apm", "key": "owner.nope", "value": "x", "by": "a", "action": "propose"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("不能在這裡改", r.text)


if __name__ == "__main__":
    unittest.main()

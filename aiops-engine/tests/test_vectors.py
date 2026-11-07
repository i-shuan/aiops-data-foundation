"""相似事故的向量輔助（docs/pdca/meta/2026-11-postgres-store.md）。為了確認：
1. embedding API：照 OpenAI 相容格式送、回來的向量照 index 排；沒設就不算；回錯丟出來。
2. 欄位比對為主：沒設 embedding 時結果跟以前一樣；設了，「說法不同但意思一樣」（指紋沒交集）的事故也找得到，why 寫「描述相近」。
3. 相似度不到門檻的不加分；只有向量像、分數不夠 MIN_SCORE 的不列。
4. 事故的文字沒變不重算（看文字雜湊），改了才重算；API 出錯退回只用欄位比對、不擋。
5. pgvector 有裝用 SQL 算、沒裝在程式裡算，兩條路結果一樣。"""
import os
import unittest
from unittest import mock

import llm.embedding as EMB
import llm.similar as SIM
from common.infra.repos import Repos
from common.scenario import load_scenario
from tests.pg import fresh_db, needs_pg

ENV = {"EMBEDDING_URL": "https://llm-gw.example.internal/v1", "EMBEDDING_MODEL": "test-embed", "EMBEDDING_API_KEY": "k"}
# 假的 embedding：每個「概念」一維，文字裡出現就是 1（說法不同、意思一樣的詞落在同一維）
CONCEPTS = [("連線池", "connection pool", "pool"), ("逾時", "timeout", "time out"), ("磁碟", "disk"), ("憑證", "certificate")]


def fake_embed(texts):
    return [[1.0 if any(w in t.lower() for w in words) else 0.0 for words in CONCEPTS] + [0.01] for t in texts]


def incident(title, root_cause, fp=None, status="confirmed"):
    fp = SIM.Fingerprint.of(fp or {}).to_doc()
    return {"status": status, "title": title, "root_cause": root_cause, "root_cause_category": "db", "start": "2026-10-01T00:00:00+00:00",
            "fingerprint": fp, "services": fp.get("services")}


class Client(unittest.TestCase):
    def test_request_shape_and_order(self):
        class R:
            status_code = 200
            def json(self):
                return {"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]}
        with mock.patch.dict(os.environ, ENV), mock.patch.object(EMB.requests, "post", return_value=R()) as post:
            self.assertEqual(EMB.embed(["a", "b"]), [[1.0, 0.0], [0.0, 1.0]])
        url, kw = post.call_args.args[0], post.call_args.kwargs
        self.assertEqual(url, "https://llm-gw.example.internal/v1/embeddings")
        self.assertEqual((kw["json"], kw["headers"]["Authorization"]), ({"model": "test-embed", "input": ["a", "b"]}, "Bearer k"))

    def test_not_configured_and_errors(self):
        with mock.patch.dict(os.environ, {"EMBEDDING_URL": "", "EMBEDDING_MODEL": ""}):
            self.assertFalse(EMB.configured())
            with self.assertRaises(RuntimeError):
                EMB.embed(["a"])

        class Bad:
            status_code, text = 500, "boom"
        with mock.patch.dict(os.environ, ENV), mock.patch.object(EMB.requests, "post", return_value=Bad()), self.assertRaises(RuntimeError):
            EMB.embed(["a"])

    def test_cosine_and_texts(self):
        self.assertAlmostEqual(EMB.cosine([1, 0], [1, 0]), 1.0)
        self.assertEqual(EMB.cosine([1, 0], [0, 0]), 0.0)
        self.assertEqual(EMB.cosine([1], [1, 0]), 0.0)
        t = EMB.incident_text(incident("DB 連線池耗盡", "批次沒關連線", {"slow_segments": ["a › SELECT"]}))
        self.assertIn("標題：DB 連線池耗盡", t)
        self.assertIn("慢的段：a › SELECT", t)


class Base:
    """兩條路（沒有 pgvector、有 pgvector）共用的測試；子類別給 db。"""
    def make_db(self):
        raise NotImplementedError

    def setUp(self):
        self.repo = Repos(None, load_scenario(), db=self.make_db())
        self.repo.incidents.bulk([
            ("pool", incident("Oracle connection pool 用光", "batch job 沒釋放 pool")),          # 指紋是空的：欄位比對永遠找不到
            ("disk", incident("節點磁碟滿", "log 沒輪替")),
            ("auto", incident("連線池又滿", "?", status="auto")),                                 # 還沒人確認：不拿
            ("field", incident("同一段變慢", "統計資訊", {"services": ["mes-api"], "slow_segments": ["mes-api › SELECT lots"]}))])
        self.fp = SIM.Fingerprint.of({"services": ["mes-api"], "slow_segments": ["mes-api › SELECT lots"]})
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.embed = mock.patch.object(EMB, "embed", side_effect=fake_embed)
        self.calls = self.embed.start()
        self.addCleanup(self.embed.stop)

    def test_vector_finds_same_meaning_with_different_words(self):
        got = SIM.search(self.repo, self.fp, text="mes-api 連線池滿了")
        ids = [g["incident_id"] for g in got]
        self.assertEqual(ids[0], "field")                                       # 欄位比對為主：同一段變慢的排第一
        self.assertIn("pool", ids)                                              # 指紋沒交集，但「連線池」= connection pool
        self.assertNotIn("auto", ids)
        self.assertNotIn("disk", ids)                                           # 意思不同：相似度不到門檻
        why = next(g for g in got if g["incident_id"] == "pool")["why"]
        self.assertTrue(any(w.startswith("描述相近（向量相似度") for w in why), why)

    def test_without_embedding_same_as_before(self):
        with mock.patch.dict(os.environ, {"EMBEDDING_URL": ""}):
            got = SIM.search(self.repo, self.fp, text="mes-api 連線池滿了")
        self.assertEqual([g["incident_id"] for g in got], ["field"])
        self.calls.assert_not_called()

    def test_vectors_are_reused_until_text_changes(self):
        SIM.search(self.repo, self.fp, text="連線池")
        first = sum(len(c.args[0]) for c in self.calls.call_args_list)
        self.assertEqual(first, 3 + 1)                                          # 3 件確認過的 + 查詢那一段
        SIM.search(self.repo, self.fp, text="連線池")
        self.assertEqual(sum(len(c.args[0]) for c in self.calls.call_args_list) - first, 1)    # 只算查詢的
        self.repo.incidents.patch("disk", {"root_cause": "憑證過期"})
        n = len(self.calls.call_args_list)
        SIM.search(self.repo, self.fp, text="連線池")
        self.assertEqual([len(c.args[0]) for c in self.calls.call_args_list[n:]], [1, 1])      # 改過的那件重算一次，加查詢

    def test_api_failure_falls_back_to_fields(self):
        self.calls.side_effect = RuntimeError("embedding API 回 503")
        got = SIM.search(self.repo, self.fp, text="連線池")
        self.assertEqual([g["incident_id"] for g in got], ["field"])

    def test_threshold_and_min_score(self):
        # 「連線池 逾時」跟 pool 那件的相似度約 0.71：預設門檻 0.8 不加分
        self.assertNotIn("pool", [g["incident_id"] for g in SIM.search(self.repo, self.fp, text="連線池 逾時")])
        got = SIM.search(self.repo, self.fp, text="連線池 逾時", vector={"min_similarity": 0.7, "weight": 6})
        self.assertIn("pool", [g["incident_id"] for g in got])                  # 門檻放低、權重夠：0.71 × 6 ≥ 4
        got = SIM.search(self.repo, self.fp, text="連線池", vector={"weight": 1.0})
        self.assertNotIn("pool", [g["incident_id"] for g in got])               # 只有向量像、分數不到 MIN_SCORE：不列

    def test_exclude_self(self):
        got = SIM.search(self.repo, self.fp, text="connection pool", exclude_id="pool")
        self.assertNotIn("pool", [g["incident_id"] for g in got])


@needs_pg
class WithoutPgvector(Base, unittest.TestCase):
    def make_db(self):
        db = fresh_db()
        self.assertFalse(db.has_vector())
        return db


@needs_pg
class WithPgvector(Base, unittest.TestCase):
    def make_db(self):
        db = fresh_db()
        try:
            db.execute("CREATE EXTENSION vector")
        except Exception as e:  # noqa: BLE001  本機沒裝 pgvector：這條路略過
            raise unittest.SkipTest(f"沒有 pgvector：{type(e).__name__}") from None
        self.assertTrue(db.has_vector())
        return db

    def test_sql_path_is_used(self):
        SIM.search(self.repo, self.fp, text="連線池")
        n = self.repo.db.query("SELECT count(*) AS n FROM aiops_incident_vectors WHERE embedding IS NOT NULL")[0]["n"]
        self.assertEqual(n, 3)


if __name__ == "__main__":
    unittest.main()

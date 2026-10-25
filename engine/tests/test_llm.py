"""LLM 摘要（core.llm）：輸入包的 id、每句要有依據的檢查、固定句型、模型掛了退回固定句型。不叫真的模型。"""
import unittest
from unittest import mock

from core import llm as LLM

ALERT = {"alert_id": "a-1", "service": "mes-api", "severity": "critical", "started_at": "2026-10-25T02:00:00+00:00", "minutes": 12,
         "routes": ["GET /lots/{id}"], "dept": "D3120"}
FINDINGS = {"anomalies": [{"series": "http.latency_p95_s", "value": 3.1, "median": 0.2, "z": 20}],
            "segments": [{"segment": "mes-api › wip-worker › Oracle SELECT lots", "now_ms": 1500.4, "ref_ms": 30.2, "delta_ms": 1470.2}],
            "error_origins": [{"segment": "wip-worker › Oracle", "now_errors": 40, "ref_errors": 0}],
            "new_patterns": [{"service": "wip-worker", "pattern": "connection pool timeout after <N>ms", "now": 120, "ref": 0}]}
SIMILAR = [{"start": "2026-09-12T02:00:00+00:00", "root_cause_category": "db", "root_cause": "統計資訊過期", "action": "重跑 analyze",
            "how_detected": "只有 lots 的查詢慢", "why": ["同服務（mes-api）", "同一段變慢（…Oracle SELECT lots）"]}]


class Pack(unittest.TestCase):
    def test_ids(self):
        p = LLM.build_pack(ALERT, FINDINGS, SIMILAR)
        self.assertEqual(p["_ids"], ["A1", "E1", "I1", "L1", "S1", "T1"])
        self.assertEqual(p["segments"][0]["extra_ms"], 1470)
        self.assertEqual(p["similar"][0]["action"], "重跑 analyze")

    def test_no_findings_no_similar(self):
        p = LLM.build_pack(ALERT, None, [])
        self.assertEqual(p["_ids"], ["T1"])     # 這件告警本身永遠有 id，模型講「持續 10 分鐘」才有依據


class Validate(unittest.TestCase):
    pack = LLM.build_pack(ALERT, FINDINGS, SIMILAR)

    def test_ok(self):
        ok, why = LLM.validate("mes-api 變慢在 Oracle [S1]。\n跟 9/12 相似 [I1][S1]。", self.pack)
        self.assertTrue(ok, why)

    def test_sentence_without_citation_rejected(self):
        ok, why = LLM.validate("mes-api 變慢在 Oracle [S1]。\n應該是 DB 的問題。", self.pack)
        self.assertFalse(ok)
        self.assertIn("沒有依據", why)

    def test_unknown_id_rejected(self):
        ok, why = LLM.validate("跟上次一樣 [I7]。", self.pack)
        self.assertFalse(ok)
        self.assertIn("I7", why)

    def test_empty_and_too_long(self):
        self.assertFalse(LLM.validate("", self.pack)[0])
        self.assertFalse(LLM.validate("\n".join(f"第 {i} 句 [S1]" for i in range(7)), self.pack)[0])


class Template(unittest.TestCase):
    def test_template_passes_validate_and_cites_similar(self):
        pack = LLM.build_pack(ALERT, FINDINGS, SIMILAR)
        text = LLM.template(pack)
        ok, why = LLM.validate(text, pack)
        self.assertTrue(ok, why)
        self.assertIn("多花約 1470 毫秒", text)
        self.assertIn("當時的根因是 [db] 統計資訊過期，當時的處置：重跑 analyze [I1]", text)
        self.assertIn("建議先照上次的方式確認：只有 lots 的查詢慢 [I1]", text)

    def test_template_without_similar_says_so(self):
        pack = LLM.build_pack(ALERT, FINDINGS, [])
        text = LLM.template(pack)
        self.assertIn("沒有相似的過去事故", text)
        self.assertTrue(LLM.validate(text, pack)[0])

    def test_template_with_nothing_is_empty_and_not_shown(self):
        pack = LLM.build_pack(ALERT, None, [])
        r = LLM.summarize(pack, "template")
        self.assertFalse(r["ok"])
        self.assertEqual(r["text"], "")


class Summarize(unittest.TestCase):
    pack = LLM.build_pack(ALERT, FINDINGS, SIMILAR)

    def test_model_failure_falls_back_to_template(self):
        with mock.patch.object(LLM, "_call_openai", side_effect=RuntimeError("401 Unauthorized")):
            r = LLM.summarize(self.pack, "openai")
        self.assertTrue(r["ok"])
        self.assertIn("template（openai 失敗", r["provider"])
        self.assertIn("[S1]", r["text"])

    def test_model_output_with_fake_citation_is_hidden(self):
        with mock.patch.object(LLM, "_call_anthropic", return_value="是 Redis 的問題 [R9]。"):
            r = LLM.summarize(self.pack, "anthropic")
        self.assertFalse(r["ok"])
        self.assertEqual(r["text"], "")
        self.assertIn("R9", r["reason"])

    def test_model_empty_output_falls_back_to_template(self):
        with mock.patch.object(LLM, "_call_openai", return_value="  \n"):
            r = LLM.summarize(self.pack, "openai")
        self.assertTrue(r["ok"])
        self.assertIn("回空的", r["provider"])
        self.assertIn("[S1]", r["text"])

    def test_model_output_ok_is_kept(self):
        with mock.patch.object(LLM, "_call_anthropic", return_value="Oracle 那段多花 1.4 秒 [S1]。\n建議先重跑 analyze [I1]。"):
            r = LLM.summarize(self.pack, "anthropic")
        self.assertTrue(r["ok"])
        self.assertEqual(r["provider"], "anthropic")


if __name__ == "__main__":
    unittest.main()


class BadInputs(unittest.TestCase):
    """docs/pdca/2026-10-fix-critical.md 1：根因分析沒有對照組時 delta_ms 是 NaN、異常的 value 可能是 None，摘要不能因此丟例外。"""

    def test_nan_segment_and_none_value_do_not_raise(self):
        f = {"anomalies": [{"series": "http.latency_p95_s", "value": None, "median": float("nan"), "z": None}],
             "segments": [{"segment": "mes-api › Oracle", "now_ms": 1500.0, "ref_ms": float("nan"), "delta_ms": float("nan")}],
             "error_origins": [], "new_patterns": []}
        pack = LLM.build_pack(ALERT, f, [])
        self.assertIsNone(pack["segments"][0]["extra_ms"])
        self.assertIsNone(pack["anomalies"][0]["value"])
        r = LLM.summarize(pack, "template")      # 不丟例外；有沒有句子都可以
        self.assertIn("ok", r)
        text = LLM.template(pack)
        self.assertTrue(LLM.validate(text, pack)[0] or not text)


class SegmentsThatDidNotSlowDown(unittest.TestCase):
    """docs/pdca/2026-10-fix-feedback-ux.md 8：有段但沒變慢（extra_ms ≤ 0）、沒有錯誤源頭時，摘要不能空、不能叫人去查變快的那段。"""
    f = {"anomalies": [{"series": "http.latency_p95_s", "value": 3.1, "median": 0.2, "z": 20}],
         "segments": [{"segment": "mes-api › redis GET", "now_ms": 10.0, "ref_ms": 22.0, "delta_ms": -12.0}], "error_origins": [], "new_patterns": []}

    def test_anomaly_sentence_still_written(self):
        pack = LLM.build_pack(ALERT, self.f, [])
        text = LLM.template(pack)
        self.assertIn("http.latency_p95_s 異常：現在 3.1", text)
        self.assertNotIn("redis", text)                 # 變快的那段不提、不叫人去查它
        self.assertIn("trace 也沒看到哪一段變慢", text)
        ok, why = LLM.validate(text, pack)
        self.assertTrue(ok, why)
        self.assertTrue(LLM.summarize(pack, "template")["ok"])

    def test_with_similar_uses_similar(self):
        text = LLM.template(LLM.build_pack(ALERT, self.f, SIMILAR))
        self.assertIn("[I1]", text)
        self.assertNotIn("redis", text)

"""回饋的自然語言抽取（core.llm.extract_feedback）與對到調整種類（detector.feedback.suggest_from_text、tuning.propose_from_text）。
不叫真的模型；解析率用 tests/feedback_extract_eval.py 量。docs/pdca/phase5-learning-loop/2026-10-feedback-nl-extract.md。"""
import json
import unittest
from unittest import mock

import pandas as pd

from core import llm as LLM
from detector import feedback as F
from detector import tuning as T
from detector.adjust import active, problem

ALERT = {"alert_id": "a-1", "service": "mes-api", "series": ["http.latency_p95_s", "http.error_rate"], "title": "mes-api 變慢"}
GOOD = {"feedback_type": "false_positive", "identified_cause": "scheduled_batch_job", "target_service": "mes-api", "suggested_action": "suppress_during_cron"}


class Validate(unittest.TestCase):
    def test_good(self):
        fields, why = LLM.validate_extraction(GOOD, ALERT)
        self.assertEqual((fields, why), (GOOD, ""))

    def test_missing_field(self):
        fields, why = LLM.validate_extraction({k: v for k, v in GOOD.items() if k != "suggested_action"}, ALERT)
        self.assertIsNone(fields)
        self.assertIn("缺欄位", why)

    def test_unknown_enum_values(self):
        self.assertIsNone(LLM.validate_extraction({**GOOD, "feedback_type": "maybe"}, ALERT)[0])
        self.assertIsNone(LLM.validate_extraction({**GOOD, "suggested_action": "restart_pod"}, ALERT)[0])

    def test_cause_normalized_or_rejected(self):
        # 大小寫、空白、減號正規化成 snake_case；中文、太長、含標點的不是短代號
        self.assertEqual(LLM.validate_extraction({**GOOD, "identified_cause": " Scheduled Batch-Job "}, ALERT)[0]["identified_cause"], "scheduled_batch_job")
        self.assertIsNone(LLM.validate_extraction({**GOOD, "identified_cause": "月底批次"}, ALERT)[0])
        self.assertIsNone(LLM.validate_extraction({**GOOD, "identified_cause": "x" * 50}, ALERT)[0])
        self.assertIsNone(LLM.validate_extraction({**GOOD, "identified_cause": ""}, ALERT)[0])

    def test_target_service_defaults_to_alert_and_rejects_junk(self):
        self.assertEqual(LLM.validate_extraction({**GOOD, "target_service": ""}, ALERT)[0]["target_service"], "mes-api")
        self.assertEqual(LLM.validate_extraction({**GOOD, "target_service": None}, ALERT)[0]["target_service"], "mes-api")
        self.assertEqual(LLM.validate_extraction({**GOOD, "target_service": "wip-worker"}, ALERT)[0]["target_service"], "wip-worker")
        self.assertIsNone(LLM.validate_extraction({**GOOD, "target_service": ""}, {})[0])              # 告警也沒服務：不能空
        self.assertIsNone(LLM.validate_extraction({**GOOD, "target_service": "the mes api"}, ALERT)[0])  # 有空白不像服務名

    def test_extra_keys_dropped_and_non_dict_rejected(self):
        self.assertEqual(set(LLM.validate_extraction({**GOOD, "confidence": 0.9}, ALERT)[0]), set(LLM.EXTRACT_FIELDS))
        self.assertIsNone(LLM.validate_extraction(["not", "a", "dict"], ALERT)[0])


class ParseJson(unittest.TestCase):
    def test_fenced_and_chatty(self):
        self.assertEqual(LLM.parse_json_object("```json\n{\"a\": 1}\n```"), {"a": 1})
        self.assertEqual(LLM.parse_json_object("好的，結果如下：{\"a\": 1} 以上。"), {"a": 1})

    def test_not_json(self):
        self.assertIsNone(LLM.parse_json_object("我覺得是誤報"))
        self.assertIsNone(LLM.parse_json_object("[1, 2]"))
        self.assertIsNone(LLM.parse_json_object(""))


class Extract(unittest.TestCase):
    TEXT = "這是每月初的批次匯出報表，屬於預期內負載。"

    def test_model_json_is_validated_and_kept(self):
        with mock.patch.object(LLM, "_call_openai", return_value=json.dumps(GOOD)):
            r = LLM.extract_feedback(self.TEXT, ALERT, "openai")
        self.assertEqual((r["ok"], r["fields"], r["provider"]), (True, GOOD, "openai"))

    def test_model_garbage_means_no_extraction(self):
        # 模型回了但不是 JSON、或欄位值不合法：不抽、不猜（只存原文）
        with mock.patch.object(LLM, "_call_openai", return_value="我覺得是月底批次造成的誤報。"):
            r = LLM.extract_feedback(self.TEXT, ALERT, "openai")
        self.assertEqual((r["ok"], r["fields"]), (False, None))
        self.assertIn("不是 JSON", r["reason"])
        with mock.patch.object(LLM, "_call_openai", return_value=json.dumps({**GOOD, "suggested_action": "mute_forever"})):
            r = LLM.extract_feedback(self.TEXT, ALERT, "openai")
        self.assertFalse(r["ok"])
        self.assertIn("suggested_action", r["reason"])

    def test_model_down_falls_back_to_template(self):
        with mock.patch.object(LLM, "_call_openai", side_effect=RuntimeError("401 Unauthorized")):
            r = LLM.extract_feedback(self.TEXT, ALERT, "openai")
        self.assertTrue(r["ok"])
        self.assertIn("template（openai 失敗", r["provider"])
        self.assertEqual(r["fields"]["suggested_action"], "suppress_during_cron")
        with mock.patch.object(LLM, "_call_anthropic", return_value="  "):
            r = LLM.extract_feedback(self.TEXT, ALERT, "anthropic")
        self.assertIn("回空的", r["provider"])

    def test_empty_text_and_never_raises(self):
        self.assertFalse(LLM.extract_feedback("   ", ALERT, "openai")["ok"])
        with mock.patch.object(LLM, "template_extract", side_effect=KeyError("boom")):
            r = LLM.extract_feedback(self.TEXT, ALERT, "template")
        self.assertFalse(r["ok"])
        self.assertIn("KeyError", r["reason"])

    def test_template_covers_the_example_sentences(self):
        t = lambda s, svc="mes-api": LLM.template_extract(s, {"service": svc})
        self.assertEqual(t(self.TEXT), GOOD)
        self.assertEqual(t("根因報錯了，不是 mes-api 自己慢，是 wip-worker 的連線池被打滿。"),
                         {"feedback_type": "wrong_root_cause", "identified_cause": "db_connection_pool", "target_service": "wip-worker", "suggested_action": "none"})
        self.assertEqual(t("這支 API 本來就慢，每次都跳，門檻放寬一點。")["suggested_action"], "widen_threshold")
        self.assertEqual(t("DNS 解析間歇失敗造成的；這種錯以後要能直接抓到。")["suggested_action"], "add_rule")
        self.assertEqual(t("這件是真的，但不用調，處理完了。"), {"feedback_type": "true_positive", "identified_cause": "unknown", "target_service": "mes-api", "suggested_action": "none"})

    def test_prompt_carries_alert_service_and_text(self):
        seen = {}
        with mock.patch.object(LLM, "_call_openai", side_effect=lambda p: seen.setdefault("p", p) and json.dumps(GOOD)):
            LLM.extract_feedback(self.TEXT, ALERT, "openai")
        self.assertIn("mes-api", seen["p"])
        self.assertIn(self.TEXT, seen["p"])
        self.assertIn("scheduled_batch_job", seen["p"])


class MapToTuning(unittest.TestCase):
    """suggested_action → 既有的調整種類；都是 proposed、source feedback、reason 帶原文；不自動生效。"""
    TEXT = "這是每月初的批次匯出報表，屬於預期內負載。"

    def test_suppress_during_cron_is_recurring_override_without_guessing_schedule(self):
        s = F.suggest_from_text(GOOD, ALERT, self.TEXT)
        self.assertEqual((s["kind"], s["status"], s["source"], s["effect"], s["alert_id"]), ("recurring_override", "proposed", "feedback", {"mute": True}, "a-1"))
        self.assertEqual(s["match"], {"service": "^mes\\-api$"})
        self.assertNotIn("schedule", s)
        self.assertIn(self.TEXT, s["reason"])
        self.assertIn("缺 schedule", s["reason"])
        self.assertEqual(s["missing"], ["schedule.cron", "schedule.duration"])
        # 沒有 schedule：adjust.load 會標「cron 要 5 欄」略過，所以補齊前絕不會生效
        self.assertIn("cron 要 5 欄", problem(s))
        self.assertEqual(active([s], pd.Timestamp("2026-10-29", tz="UTC")), [])

    def test_widen_threshold_is_override_on_alert_series_and_target_service(self):
        f = {**GOOD, "identified_cause": "known_slow", "suggested_action": "widen_threshold"}
        s = F.suggest_from_text(f, ALERT, "本來就慢", current_z=5.0)
        self.assertEqual((s["kind"], s["z"], s["status"]), ("override", 7.0, "proposed"))
        self.assertEqual(s["series"], "http\\.latency_p95_s|http\\.error_rate")
        self.assertEqual(s["match"], {"service": "^mes\\-api$"})
        self.assertIsNone(problem(s))
        self.assertEqual(active([s], pd.Timestamp("2026-10-29", tz="UTC"), kinds=("override",)), [])   # proposed 不生效
        # 文字點名別的服務：建議對的是那個服務
        s2 = F.suggest_from_text({**f, "target_service": "wip-worker"}, ALERT, "x")
        self.assertEqual(s2["match"], {"service": "^wip\\-worker$"})
        self.assertIsNone(F.suggest_from_text(f, {**ALERT, "series": []}, "x")["series"])

    def test_add_rule_is_rule_suggestion_without_threshold(self):
        f = {**GOOD, "feedback_type": "true_positive", "identified_cause": "external_api", "suggested_action": "add_rule"}
        s = F.suggest_from_text(f, ALERT, "外部中斷以後要直接告警")
        self.assertEqual((s["kind"], s["service"], s["root_cause_category"], s["incident_ids"]), ("rule_suggestion", "mes-api", "external", ["a-1"]))
        self.assertIsNone(s["rule_draft"]["threshold"])          # 算不出門檻 → cmd_adjust approve 會擋
        self.assertIn("不能核准", s["rule_draft"]["_note"])
        self.assertEqual(F.suggest_from_text({**f, "identified_cause": "unknown"}, ALERT, "x")["root_cause_category"], "other")

    def test_none_is_nothing(self):
        self.assertIsNone(F.suggest_from_text({**GOOD, "suggested_action": "none"}, ALERT, "x"))

    def test_propose_writes_proposed_row_with_stable_id(self):
        class ES:
            def __init__(self):
                self.put = {}
            def ensure_index(self, name, body):
                self.ensured = name
            def req(self, method, path, body=None, ok=(200, 201)):
                self.put[path] = body
                return {}
        es = ES()
        now = pd.Timestamp("2026-10-29T01:00:00Z")
        _id = T.propose_from_text(es, GOOD, ALERT, self.TEXT, "amy", now)
        self.assertEqual(es.ensured, T.TUNING_INDEX)
        (path, body), = es.put.items()
        self.assertEqual(path, f"/{T.TUNING_INDEX}/_doc/{_id}?refresh=true")
        self.assertEqual((body["status"], body["by"], body["at"], body["kind"]), ("proposed", "amy", now.isoformat(), "recurring_override"))
        self.assertEqual(T.propose_from_text(es, GOOD, ALERT, self.TEXT, "amy", now), _id)              # 同一人、同一件、同一種：覆蓋不重複
        self.assertNotEqual(T.propose_from_text(es, GOOD, ALERT, self.TEXT, "bob", now), _id)
        self.assertIsNone(T.propose_from_text(es, {**GOOD, "suggested_action": "none"}, ALERT, self.TEXT, "amy", now))


if __name__ == "__main__":
    unittest.main()

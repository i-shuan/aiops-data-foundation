"""功能積木（common/blocks.py，docs/pdca/meta/2026-11-blocks.md）。為了確認：
1. 登記表：沒寫 blocks 的段 = 預設（跟以前一樣）；寫錯名字講清楚有哪些；只能一塊的段多選會擋；自己寫的（套件.模組:名字）載得到。
2. 情境資料檔的 blocks: 讀得進 Scenario；`detector blocks` 列得出每一段開了哪幾塊。
3. ④ 關掉的判斷指令只印一行、回 0、不碰資料；自己寫的判斷加得進 detector 的子命令。
4. ⑥ 嚴重度：拿掉 p0 就不升級；自己寫的接在後面會改結果。併件換成自己寫的會被叫。
5. ⑦ 相似事故只開 fields 時不算向量；自己寫的檢索的候選會進分數；歷史 case 的積木照順序出現；摘要可以整個關掉；報告段落可以填自己寫的。
6. ⑨ 只跑開著的建議規則，自己寫的也會跑；from_text 關掉時一句話不提調整。"""
import contextlib
import dataclasses
import io
import os
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from unittest import mock

import common.blocks as BLOCKS
import incident.alert_service as C
import learn.feedback_service as FS
import llm.embedding as EMB
import llm.similar as SIM
import tests.blocks_fixtures as FX
from common.scenario import load_scenario
from common.scenario.base import _read, from_dict
from incident.config.rca import Alerts
from tests.config_factory import rca
from tests.fakes import MemoryRepos

T0 = datetime(2026, 11, 7, 2, 0, tzinfo=timezone.utc)


def with_blocks(**blocks):
    return dataclasses.replace(load_scenario(), blocks=blocks)


class Registry(unittest.TestCase):
    def test_defaults_are_everything_like_before(self):
        sc = load_scenario()
        self.assertEqual(BLOCKS.enabled("detect", sc), list(BLOCKS.stage("detect").builtin))
        self.assertEqual(BLOCKS.enabled("severity", sc), ["golden_signals", "p0"])
        self.assertEqual(BLOCKS.enabled("rca_sections", sc), list(sc.sections))          # 沒寫 blocks：沿用情境的 sections

    def test_bad_names_one_only_and_custom(self):
        with self.assertRaises(ValueError) as cm:
            BLOCKS.enabled("detect", with_blocks(detect=["dynamic_baselin"]))
        self.assertIn("dynamic_baseline", str(cm.exception))                              # 講出有哪些
        with self.assertRaises(ValueError):
            BLOCKS.enabled("grouping", with_blocks(grouping=["same_entity", "tests.blocks_fixtures:merge"]))
        self.assertIs(BLOCKS.stage("severity").load("tests.blocks_fixtures:always_critical"), FX.always_critical)
        with self.assertRaises(ValueError):
            BLOCKS.stage("severity").load("tests.blocks_fixtures:nope")
        self.assertEqual(BLOCKS.enabled("summary", with_blocks(summary=[])), [])         # 空清單 = 這段全關

    def test_every_builtin_loads(self):
        for st in BLOCKS.STAGES.values():
            for name in st.builtin:
                with self.subTest(stage=st.key, name=name):
                    self.assertTrue(callable(st.load(name)))

    def test_yaml_and_describe(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write("extends: it_otel\nname: blocks-test\nblocks:\n  detect: [dynamic_baseline]\n  severity: [golden_signals]\n")
        self.addCleanup(os.unlink, f.name)
        sc = from_dict(_read(f.name))
        self.assertEqual(sc.blocks, {"detect": ["dynamic_baseline"], "severity": ["golden_signals"]})
        text = "\n".join(BLOCKS.describe(sc))
        self.assertIn("✓ dynamic_baseline", text)
        self.assertIn("· peer_compare", text)
        self.assertIn("· p0", text)
        self.assertIn("自己寫的積木", text)


class Detect(unittest.TestCase):
    def run_cli(self, yaml_text, argv):
        import interface.cli.detect as D
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write("extends: it_otel\nname: blocks-test\n" + yaml_text)
        self.addCleanup(os.unlink, f.name)
        with mock.patch.dict(os.environ, {"AIOPS_PLUGIN": f.name}), contextlib.redirect_stdout(io.StringIO()) as out, \
             mock.patch("detect.strategy.threshold_rule.cmd_rules", side_effect=AssertionError("不該跑")):
            rc = D.main(argv)
        return rc, out.getvalue()

    def test_disabled_command_skips(self):
        rc, out = self.run_cli("blocks:\n  detect: [dynamic_baseline]\n", ["rules", "--config", "configs/baseline/rules.yaml"])
        self.assertEqual(rc, 0)
        self.assertIn("④ 積木 threshold_rule 沒開", out)

    def test_custom_detect_block_adds_command(self):
        rc, out = self.run_cli("blocks:\n  detect: [dynamic_baseline, tests.blocks_fixtures:register]\n", ["my-check"])
        self.assertEqual(rc, 0)
        self.assertIn("my-check 跑了", out)


class Severity(unittest.TestCase):
    def grade(self, sc):
        anoms = [{"_id": f"a{i}", "@timestamp": (T0 + timedelta(minutes=i)).isoformat(), "series": "http.latency_p95_s",
                  "keys": {"service": "pay-api"}} for i in range(2)]
        al = C.Alert(alert_id="a", category="service", service="pay-api", status="open", severity="warning", started_at=T0, updated_at=T0,
                     anomaly_ids=[a["_id"] for a in anoms], series=["http.latency_p95_s"], routes=[], new=True, anoms=anoms)
        C.grade_alert(MemoryRepos(scenario=sc), rca(indices={}, alerts={"deploy_grace": False, "p0_services": ["pay-api"]}), al, T0 + timedelta(minutes=3))
        return al

    def test_chain(self):
        self.assertEqual(self.grade(with_blocks()).severity, "high")                      # 預設：warning → P0 升一級
        self.assertEqual(self.grade(with_blocks(severity=["golden_signals"])).severity, "warning")
        al = self.grade(with_blocks(severity=["golden_signals", "tests.blocks_fixtures:always_critical"]))
        self.assertEqual(al.severity, "critical")
        self.assertIn("自己寫的積木", al.severity_why)

    def test_grouping_is_a_block(self):
        FX.CALLS.clear()
        (_, merge), = BLOCKS.stage("grouping").loaded(with_blocks(grouping=["tests.blocks_fixtures:merge"]))
        self.assertEqual(merge([], [], T0, timedelta(minutes=30)), [])
        self.assertEqual(FX.CALLS, ["merge"])
        (_, merge), = BLOCKS.stage("grouping").loaded(with_blocks())
        self.assertIs(merge, C.AL.merge)


class Rca(unittest.TestCase):
    FP = {"services": ["mes-api"], "slow_segments": ["mes-api › SELECT lots"]}
    INC = ("f-1", {"status": "confirmed", "title": "同一段", "root_cause_category": "db", "start": "2026-10-01T00:00:00+00:00",
                   "fingerprint": SIM.Fingerprint.of(FP).to_doc(), "checked": ["DB 連線池"]})

    def test_fields_only_never_embeds(self):
        repo = MemoryRepos(scenario=with_blocks(similar=["fields"]), incidents=[self.INC])
        with mock.patch.dict(os.environ, {"EMBEDDING_URL": "https://x/v1", "EMBEDDING_MODEL": "m"}), \
             mock.patch.object(EMB, "embed", side_effect=AssertionError("不該算向量")):
            got = SIM.search(repo, self.FP, text="連線池")
        self.assertEqual([g["incident_id"] for g in got], ["f-1"])

    def test_custom_retriever_joins_the_ranking(self):
        repo = MemoryRepos(scenario=with_blocks(similar=["fields", "tests.blocks_fixtures:by_ticket"]), incidents=[self.INC])
        got = {g["incident_id"]: g for g in SIM.search(repo, self.FP)}
        self.assertEqual(set(got), {"f-1", "t-1"})
        self.assertTrue(any(w.startswith("描述相近") for w in got["t-1"]["why"]))         # 0.9 × 5 = 4.5 分，夠

    def test_hints_order_and_off(self):
        findings = {"segments": [{"segment": "mes-api › SELECT lots", "delta_ms": 100, "ref_ms": 10}], "error_origins": []}
        repo = MemoryRepos(scenario=with_blocks(hints=["tests.blocks_fixtures:my_hint", "checklist"]), incidents=[self.INC])
        lines = C._case_hints(repo, Alerts(), dict(findings), {"category": "db"})
        self.assertEqual(lines[0], "## 自己寫的線索")
        self.assertIn("- [ ] DB 連線池（過去 1 件有查）", lines)
        self.assertFalse(any("歷史 case 的線索" in l for l in lines))                     # segment_hints 沒開
        self.assertEqual(C._case_hints(MemoryRepos(scenario=with_blocks(hints=[])), Alerts(), dict(findings), None), [])

    def test_summary_off_and_custom_section(self):
        self.assertEqual(BLOCKS.stage("summary").loaded(with_blocks(summary=[])), [])
        sc = with_blocks(rca_sections=["anomalies", "tests.blocks_fixtures:my_section"])
        self.assertEqual(BLOCKS.enabled("rca_sections", sc), ["anomalies", "tests.blocks_fixtures:my_section"])
        c = type("Ctx", (), {"out": []})()
        BLOCKS.stage("rca_sections").load("tests.blocks_fixtures:my_section")(c)
        self.assertEqual(c.out[0], "## 自己寫的段落")


class Suggest(unittest.TestCase):
    def test_only_enabled_rules_run(self):
        FX.CALLS.clear()
        repo = MemoryRepos(scenario=with_blocks(suggest=["missed", "tests.blocks_fixtures:my_suggest"]))
        from common.config import load_config
        series = load_config("configs/baseline/series.yaml", "series")
        with mock.patch.object(FS, "_connect", return_value=repo), \
             mock.patch.object(FS, "_suggest_missed", return_value=2) as missed, \
             mock.patch.object(FS, "_suggest_false_positives", side_effect=AssertionError("沒開")), \
             mock.patch.object(FS, "_suggest_precision", side_effect=AssertionError("沒開")), \
             mock.patch.object(FS, "_suggest_rules", side_effect=AssertionError("沒開")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            FS.feedback_suggest(series, Namespace(days=28, min_false_positive=3, min_recurring=3))
        missed.assert_called_once()
        self.assertEqual(FX.CALLS, ["suggest"])
        self.assertIn("9 筆建議", out.getvalue())

    def test_from_text_off(self):
        repo = MemoryRepos(scenario=with_blocks(suggest=["false_positive"]))
        ext = {"ok": True, "fields": {"feedback_type": "false_positive", "suggested_action": "suppress_during_cron"}, "provider": "template", "reason": ""}
        base = {}
        with mock.patch.object(FS.LLM, "extract_feedback", return_value=ext), \
             mock.patch.object(FS, "propose_from_text", side_effect=AssertionError("不該提")):
            got = FS._extract_text(repo, "月底批次", {"alert_id": "a", "service": "x"}, "amy", T0, base)
        self.assertEqual(got, (None, None))
        self.assertEqual(base["feedback_text"], "月底批次")                               # 原文照存


if __name__ == "__main__":
    unittest.main()

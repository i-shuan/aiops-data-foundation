"""core/config：每一份設定檔都過得了驗證、回傳跟 yaml 一樣；鍵打錯、型別錯會擋下來，訊息有檔名與欄位路徑。
模型跟功能住在一起（detector/baseline/config.py…），這裡依檔名對到模型。"""
import glob
import os
import tempfile
import unittest

import yaml

from core.config import ConfigError, load, validate
from detector.adjust.config import AdjustmentsConfig, CalendarConfig
from detector.baseline.config import SeriesConfig
from detector.prune import PruneConfig
from detector.rules.config import RulesConfig
from investigator.rca.config import RcaConfig
from plugins.factory.ingest.config import IngestConfig
from plugins.factory.paths.config import PathsConfig
from plugins.observability.chronic.config import ChronicConfig
from plugins.observability.peer.config import PeerConfig
from plugins.observability.quality.config import QualityConfig
from plugins.observability.trace.config import TraceConfig
from scheduler.config import ScheduleConfig

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, "..")
# 檔名（stem）→ 模型：configs/ 底下的每一份、tests/ 給 e2e 用的那幾份
MODELS = {"series": SeriesConfig, "rules": RulesConfig, "peer": PeerConfig, "quality": QualityConfig, "trace": TraceConfig,
          "rca": RcaConfig, "schedule": ScheduleConfig, "chronic": ChronicConfig, "prune": PruneConfig, "calendar": CalendarConfig,
          "adjustments": AdjustmentsConfig, "ingest": IngestConfig, "paths": PathsConfig}
FIXTURES = {"adaptive": SeriesConfig, "fine": SeriesConfig, "synth": SeriesConfig, "rca_loop": RcaConfig, "rules_loop": RulesConfig,
            "rca_chain": RcaConfig, "trace_edge": TraceConfig}


def model_of(path: str):
    stem = os.path.splitext(os.path.basename(path))[0]
    return MODELS.get(stem) or FIXTURES.get(stem)


def all_yaml() -> list[tuple[str, type]]:
    files = sorted(glob.glob(os.path.join(CODE, "configs", "**", "*.yaml"), recursive=True))
    files += [os.path.join(HERE, f"{s}.yaml") for s in FIXTURES]
    return [(f, m) for f in files if (m := model_of(f))]


class EveryConfigLoads(unittest.TestCase):
    def test_every_yaml_validates_and_is_unchanged(self):
        """configs/ 的 23 份、tests/ 的 7 份：驗證過，而且回傳的 dict 跟 yaml.safe_load 一模一樣（不塞預設值、_anchor 原樣）。"""
        files = all_yaml()
        self.assertGreaterEqual(len(files), 30)
        self.assertEqual({m for _, m in files}, set(MODELS.values()), "每一種設定檔至少要有一份實際的 yaml 被驗過")
        for path, model in files:
            with self.subTest(path=os.path.relpath(path, CODE), model=model.__name__):
                with open(path, encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
                got = load(path, model)
                self.assertEqual(got, raw)
                self.assertEqual(list(got), list(raw))       # 順序也一樣

    def test_anchor_keys_pass_through(self):
        got = load(os.path.join(CODE, "configs", "baseline", "rules.yaml"), RulesConfig)
        self.assertEqual(got["_svc"], "resource.attributes.service.name")
        self.assertEqual(got["rules"][0]["by"][0], got["_svc"])


class BadConfigsAreCaught(unittest.TestCase):
    def _write(self, name: str, text: str) -> str:
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_typo_key_names_file_path_and_allowed_fields(self):
        """peer.yaml 把 min_peers 打成 min_peer：啟動就擋，訊息有檔名、checks[0].min_peer、這一層能寫什麼。"""
        with open(os.path.join(CODE, "configs", "baseline", "peer.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg["checks"][0]["min_peer"] = 3
        path = self._write("peer.yaml", yaml.safe_dump(cfg, allow_unicode=True))
        with self.assertRaises(ConfigError) as cm:
            load(path, PeerConfig)
        msg = str(cm.exception)
        self.assertIn(path, msg)
        self.assertIn("checks[0].min_peer", msg)
        self.assertIn("min_volume", msg)         # 列出能寫的欄位，人一看就知道該改成什麼

    def test_wrong_type_names_field_path(self):
        path = self._write("series.yaml", "interval: 5m\nhistory_weeks: four\nseries: []\n")
        with self.assertRaises(ConfigError) as cm:
            load(path, SeriesConfig)
        self.assertIn("history_weeks", str(cm.exception))
        self.assertIn("'four'", str(cm.exception))

    def test_nested_paths(self):
        with self.assertRaises(ConfigError) as cm:
            validate({"adaptive": {"min_events": {"rate": 20, "cnt": 10}}}, SeriesConfig, "s.yaml")
        self.assertIn("s.yaml: adaptive.min_events.cnt", str(cm.exception))
        with self.assertRaises(ConfigError) as cm:
            validate({"alerts": {"severity": {"windw": 3}}}, RcaConfig, "r.yaml")
        self.assertIn("alerts.severity.windw", str(cm.exception))
        self.assertIn("deploy_grace", str(cm.exception))

    def test_ranges_and_enums(self):
        bad = [(SeriesConfig, {"history_weeks": 0}, "history_weeks"),
               (SeriesConfig, {"interval": "5 min"}, "interval"),
               (SeriesConfig, {"status": {"instant_z": -1}}, "status.instant_z"),
               (RulesConfig, {"rules": [{"name": "r", "type": "bogus", "index": "i", "by": [], "threshold": 1}]}, "rules[0].type"),
               (RulesConfig, {"rules": [{"name": "r", "type": "ratio", "index": "i", "by": [], "threshold": 1, "field": "f"}]}, "den"),
               (PeerConfig, {"checks": [{"name": "c", "index": "i", "by": ["a", "b"], "keys": ["a"], "value": "count"}]}, "keys 有 1 個、by 有 2 個"),
               (QualityConfig, {"checks": [{"name": "q", "type": "lag", "index": "i", "to": "b"}]}, "要有 from"),
               (ScheduleConfig, {"jobs": [{"name": "j", "cron": "* * * * *", "args": [], "queue": "fast"}]}, "jobs[0].queue"),
               (CalendarConfig, {"calendar": [{"name": "x"}]}, "date"),
               (IngestConfig, {"inputs": [{"name": "m", "index": "i", "series": "s", "keys": {"a": "b"}, "level": "nope"}]}, "inputs[0].level")]
        for model, raw, expect in bad:
            with self.subTest(model=model.__name__, expect=expect):
                with self.assertRaises(ConfigError) as cm:
                    validate(raw, model)
                self.assertIn(expect, str(cm.exception))

    def test_not_a_mapping(self):
        path = self._write("series.yaml", "- a\n- b\n")
        with self.assertRaises(ConfigError) as cm:
            load(path, SeriesConfig)
        self.assertIn("最上層要是 mapping", str(cm.exception))

    def test_schedule_goes_through_loader(self):
        """scheduler/runner.load_schedule 用 core.config.load：排程表的鍵打錯在這裡就擋。"""
        from scheduler.runner import load_schedule
        path = self._write("schedule.yaml", "timezone: Asia/Taipei\njobs:\n  - name: a\n    cron: '* * * * *'\n    args: [detector, rules]\n    timeout: 50\n    retry: 1\n")
        with self.assertRaises(ConfigError) as cm:
            load_schedule(path, 2)
        self.assertIn("jobs[0].retry", str(cm.exception))
        self.assertIn("retries", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

"""core/config：每一份設定檔都過得了驗證、回傳跟 yaml 一樣；鍵打錯、型別錯會擋下來，訊息有檔名與欄位路徑。"""
import glob
import os
import tempfile
import unittest

import yaml

from shared.scenario.config import KINDS, ConfigError, load_config, validate
from shared.scenario.config import MODELS

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(HERE, "..")
# 檔名 → 種類（configs/ 底下的每一份、tests/ 給 e2e 用的那幾份）
BY_NAME = {k: k for k in KINDS}
FIXTURES = {"adaptive": "series", "fine": "series", "synth": "series", "rca_loop": "rca", "rules_loop": "rules",
            "rca_chain": "rca", "trace_edge": "trace"}


def kind_of(path: str) -> str | None:
    stem = os.path.splitext(os.path.basename(path))[0]
    return BY_NAME.get(stem) or FIXTURES.get(stem)


def all_yaml() -> list[tuple[str, str]]:
    files = sorted(glob.glob(os.path.join(CODE, "configs", "**", "*.yaml"), recursive=True))
    files += [os.path.join(HERE, f"{s}.yaml") for s in FIXTURES]
    return [(f, k) for f in files if (k := kind_of(f))]


class EveryConfigLoads(unittest.TestCase):
    def test_kinds_have_models(self):
        self.assertEqual(set(MODELS), set(KINDS))

    def test_every_yaml_validates_and_is_unchanged(self):
        """configs/ 的 23 份、tests/ 的 7 份：驗證過，而且回傳的 dict 跟 yaml.safe_load 一模一樣（不塞預設值、_anchor 原樣）。"""
        files = all_yaml()
        self.assertGreaterEqual(len(files), 30)
        self.assertEqual({k for _, k in files}, set(KINDS), "每一種設定檔至少要有一份實際的 yaml 被驗過")
        for path, kind in files:
            with self.subTest(path=os.path.relpath(path, CODE), kind=kind):
                with open(path, encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
                got = load_config(path, kind)
                self.assertEqual(got, raw)
                self.assertEqual(list(got), list(raw))       # 順序也一樣

    def test_anchor_keys_pass_through(self):
        got = load_config(os.path.join(CODE, "configs", "baseline", "rules.yaml"), "rules")
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
            load_config(path, "peer")
        msg = str(cm.exception)
        self.assertIn(path, msg)
        self.assertIn("checks[0].min_peer", msg)
        self.assertIn("min_volume", msg)         # 列出能寫的欄位，人一看就知道該改成什麼

    def test_wrong_type_names_field_path(self):
        path = self._write("series.yaml", "interval: 5m\nhistory_weeks: four\nseries: []\n")
        with self.assertRaises(ConfigError) as cm:
            load_config(path, "series")
        self.assertIn("history_weeks", str(cm.exception))
        self.assertIn("'four'", str(cm.exception))

    def test_nested_paths(self):
        with self.assertRaises(ConfigError) as cm:
            validate({"adaptive": {"min_events": {"rate": 20, "cnt": 10}}}, "series", "s.yaml")
        self.assertIn("s.yaml: adaptive.min_events.cnt", str(cm.exception))
        with self.assertRaises(ConfigError) as cm:
            validate({"alerts": {"severity": {"windw": 3}}}, "rca", "r.yaml")
        self.assertIn("alerts.severity.windw", str(cm.exception))
        self.assertIn("deploy_grace", str(cm.exception))

    def test_ranges_and_enums(self):
        bad = [("series", {"history_weeks": 0}, "history_weeks"),
               ("series", {"interval": "5 min"}, "interval"),
               ("series", {"status": {"instant_z": -1}}, "status.instant_z"),
               ("rules", {"rules": [{"name": "r", "type": "bogus", "index": "i", "by": [], "threshold": 1}]}, "rules[0].type"),
               ("rules", {"rules": [{"name": "r", "type": "ratio", "index": "i", "by": [], "threshold": 1, "field": "f"}]}, "den"),
               ("peer", {"checks": [{"name": "c", "index": "i", "by": ["a", "b"], "keys": ["a"], "value": "count"}]}, "keys 有 1 個、by 有 2 個"),
               ("quality", {"checks": [{"name": "q", "type": "lag", "index": "i", "to": "b"}]}, "要有 from"),
               ("schedule", {"jobs": [{"name": "j", "cron": "* * * * *", "args": [], "queue": "fast"}]}, "jobs[0].queue"),
               ("calendar", {"calendar": [{"name": "x"}]}, "date"),
               ("ingest", {"inputs": [{"name": "m", "index": "i", "series": "s", "keys": {"a": "b"}, "level": "nope"}]}, "inputs[0].level")]
        for kind, raw, expect in bad:
            with self.subTest(kind=kind, expect=expect):
                with self.assertRaises(ConfigError) as cm:
                    validate(raw, kind)
                self.assertIn(expect, str(cm.exception))

    def test_unknown_kind(self):
        with self.assertRaises(ValueError) as cm:
            load_config(os.path.join(CODE, "configs", "baseline", "peer.yaml"), "peers")
        self.assertIn("peers", str(cm.exception))
        self.assertNotIsInstance(cm.exception, ConfigError)

    def test_schedule_goes_through_loader(self):
        """scheduler/runner.load_schedule 用 load_config：排程表的鍵打錯在這裡就擋。"""
        from interface.jobs.runner import load_schedule
        path = self._write("schedule.yaml", "timezone: Asia/Taipei\njobs:\n  - name: a\n    cron: '* * * * *'\n    args: [detector, rules]\n    timeout: 50\n    retry: 1\n")
        with self.assertRaises(ConfigError) as cm:
            load_schedule(path, 2)
        self.assertIn("jobs[0].retry", str(cm.exception))
        self.assertIn("retries", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

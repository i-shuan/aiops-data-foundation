"""common/config：每一份設定檔都過得了驗證、回傳那一種的模型（寫的鍵跟 yaml 一樣）；鍵打錯、型別錯會擋下來，訊息有檔名與欄位路徑。"""
import glob
import os
import tempfile
import unittest

import yaml

from common.config import KINDS, MODELS, ConfigError, load_config, validate

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
        """configs/ 的 23 份、tests/ 的 7 份：驗證過、回傳那一種的模型，而且「yaml 有寫的鍵」（model_dump(exclude_unset)）跟 yaml.safe_load 一樣（不塞預設值；`_` 開頭的 anchor 只給 yaml 自己用，不在裡面）。"""
        files = all_yaml()
        self.assertGreaterEqual(len(files), 30)
        self.assertEqual({k for _, k in files}, set(KINDS), "每一種設定檔至少要有一份實際的 yaml 被驗過")
        for path, kind in files:
            with self.subTest(path=os.path.relpath(path, CODE), kind=kind):
                with open(path, encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
                got = load_config(path, kind)
                self.assertIsInstance(got, MODELS[kind])
                expected = {k: v for k, v in raw.items() if not str(k).startswith("_")}
                self.assertEqual(got.model_dump(exclude_unset=True, by_alias=True), expected)

    def test_yaml_anchors_are_expanded_and_not_kept(self):
        """rules.yaml 用 `_svc: &svc …` 在別處引用：yaml 展開後值在 rules 裡，`_svc` 這個鍵本身不留在模型裡。"""
        got = load_config(os.path.join(CODE, "configs", "baseline", "rules.yaml"), "rules")
        self.assertNotIn("_svc", got.model_dump())
        self.assertEqual(got.rules[0].by[0], "resource.attributes.service.name")


class ModelsAreModels(unittest.TestCase):
    """設定是模型，用屬性讀；「yaml 有寫」跟「沒寫」用 model_fields_set 分（寫 null 與沒寫意思不同的地方用）。"""

    def setUp(self):
        self.cfg = validate({"timezone": "Asia/Taipei", "days": 3}, "chronic")

    def test_attributes_and_defaults(self):
        self.assertEqual(self.cfg.days, 3)
        self.assertEqual(self.cfg.min_days, 6)                   # 沒寫：模型的預設

    def test_written_vs_unwritten(self):
        self.assertEqual(self.cfg.model_fields_set, {"timezone", "days"})
        self.assertNotIn("min_days", self.cfg.model_fields_set)

    def test_not_dict_like_any_more(self):
        for use in (lambda: self.cfg["days"], lambda: self.cfg.get("days"), lambda: self.cfg.to_dict(), lambda: {**self.cfg}):
            with self.assertRaises((TypeError, AttributeError)):
                use()

    def test_yaml_key_keys_is_the_attribute_key_names(self):
        cfg = validate({"series": [{"name": "a", "keys": ["service"], "by": ["service.name"], "index": "m-*", "value": "count"}]}, "series")
        spec = cfg.series[0]
        self.assertEqual(spec.key_names, ["service"])
        self.assertEqual(spec.model_dump(by_alias=True, exclude_unset=True)["keys"], ["service"])     # 寫出去又是 yaml 的名字


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

"""框架（core/，仿 superset-ui；docs/pdca/meta/2026-11-superset-style.md）與外掛的結構。為了確認：
1. Registry：register_value／register_loader（延遲、只跑一次）、OverwritePolicy、keys／values／entries／remove／clear。
2. Plugin／Preset：configure 合併與 replace、Preset 先登記底下的 preset 再登記自己的 plugin、unregister 反過來。
3. ControlPanelConfig：補預設、轉型、範圍、choices、不認得的參數講出有哪些、shared_controls、control_overrides、form_data_overrides。
4. PipelinePlugin：register 進三張登記表、load_run 延遲、沒有 key 不能登記、run／load_run 只能給一個。
5. 外掛的結構：MainPreset 登記了每一段的外掛、順序跟原本的預設一樣；每個外掛資料夾有 __init__、control_panel、主檔；
   golden_signals 宣告的預設跟 GradeOptions 一樣（兩邊不會悄悄分岔）。"""
import contextlib
import importlib
import io
import pathlib
import unittest

import common.stages as ST
from common.scenario import load_scenario
from core import (ControlConfig, ControlPanelConfig, ControlPanelSectionConfig, OverwritePolicy, PipelineMetadata, PipelinePlugin,
                  Plugin, Preset, Registry, get_pipeline_control_panel_registry, get_pipeline_metadata_registry,
                  get_pipeline_run_registry, registered, shared_controls)
from incident.alert_severity import GradeOptions
from plugins.plugin_severity import GoldenSignalsPipelinePlugin, P0PipelinePlugin

PLUGINS = pathlib.Path(__file__).resolve().parents[1] / "src" / "plugins"


class RegistryTest(unittest.TestCase):
    def test_value_loader_and_listing(self):
        calls = []
        r = Registry("t").register_value("a", 1).register_loader("b", lambda: calls.append(1) or 2)
        self.assertEqual((r.get("a"), r.has("b"), len(r), r.keys()), (1, True, 2, ["a", "b"]))
        self.assertEqual(calls, [])
        self.assertEqual((r.get("b"), r.get("b")), (2, 2))
        self.assertEqual(calls, [1])
        self.assertIsNone(r.get("x"))
        self.assertEqual((r.entries(), r.values()), ([("a", 1), ("b", 2)], [1, 2]))
        self.assertEqual(len(r.remove("a")), 1)
        self.assertEqual(len(r.clear()), 0)

    def test_overwrite_policy(self):
        r = Registry("t", OverwritePolicy.PROHIBIT).register_value("a", 1)
        with self.assertRaises(ValueError):
            r.register_value("a", 2)
        self.assertEqual(Registry("t").register_value("a", 1).register_value("a", 2).get("a"), 2)
        w = Registry("t", OverwritePolicy.WARN).register_value("a", 1)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            w.register_value("a", 2)
        self.assertIn('"a"', err.getvalue())


class PluginPresetTest(unittest.TestCase):
    def test_configure(self):
        p = Plugin().configure({"key": "x", "a": 1}).configure({"a": 2})
        self.assertEqual(p.config, {"key": "x", "a": 2})
        self.assertEqual(p.configure({"b": 1}, replace=True).config, {"b": 1})
        self.assertEqual(p.reset_config().config, {})

    def test_preset_order(self):
        order = []

        class P(Plugin):
            def __init__(self, name):
                super().__init__()
                self.name = name

            def register(self):
                order.append("+" + self.name)
                return self

            def unregister(self):
                order.append("-" + self.name)
                return self

        outer = Preset(name="o", presets=[Preset(plugins=[P("i")])], plugins=[P("a"), P("b")])
        outer.register()
        outer.unregister()
        self.assertEqual(order, ["+i", "+a", "+b", "-a", "-b", "-i"])


class ControlPanelTest(unittest.TestCase):
    PANEL = ControlPanelConfig(control_panel_sections=[ControlPanelSectionConfig(label="s", control_set_rows=[
        [{"name": "n", "config": ControlConfig(type="NumberControl", default=5, min=1, max=10, label="幾個")}, "lookback"],
        [{"name": "flag", "config": ControlConfig(type="CheckboxControl", default=False)},
         {"name": "mode", "config": ControlConfig(type="SelectControl", default="a", choices=("a", "b"))},
         {"name": "names", "config": ControlConfig(type="ListControl", default=[])}],
    ])])

    def test_defaults_coercion_and_shared(self):
        self.assertIn("lookback", shared_controls)
        self.assertEqual(self.PANEL.build_form_data(), {"n": 5, "lookback": "30m", "flag": False, "mode": "a", "names": []})
        d = self.PANEL.build_form_data({"n": "7", "flag": True, "mode": "b", "names": ["p"], "lookback": "2h"})
        self.assertEqual((d["n"], d["flag"], d["mode"], d["names"], d["lookback"]), (7, True, "b", ["p"], "2h"))

    def test_errors_name_the_parameter(self):
        for raw, needle in (({"n": 99}, "n"), ({"n": "x"}, "n"), ({"flag": "yes"}, "flag"), ({"mode": "z"}, "mode"), ({"names": "p"}, "names")):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as cm:
                self.PANEL.build_form_data(raw)
            self.assertIn(needle, str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            self.PANEL.build_form_data({"nn": 1})
        self.assertIn("可調的有：n、lookback", str(cm.exception))
        with self.assertRaises(ValueError):
            ControlConfig(type="Nope")
        with self.assertRaises(ValueError):
            ControlPanelConfig(control_panel_sections=[ControlPanelSectionConfig(control_set_rows=[["ghost"]])]).controls()

    def test_overrides_and_describe(self):
        panel = ControlPanelConfig(control_panel_sections=[ControlPanelSectionConfig(control_set_rows=[["lookback"]])],
                                   control_overrides={"lookback": ControlConfig(type="TextControl", default="1h")},
                                   form_data_overrides=lambda d: {**d, "extra": 1})
        self.assertEqual(panel.build_form_data(), {"lookback": "1h", "extra": 1})
        self.assertIn("n (NumberControl, 預設 5)：幾個", "\n".join(self.PANEL.describe()))


class PipelinePluginTest(unittest.TestCase):
    def test_register_and_lazy_run(self):
        loaded = []
        p = PipelinePlugin(stage="demo", metadata=PipelineMetadata(name="hi", description="說明"),
                           load_run=lambda: loaded.append(1) or (lambda: "ran")).configure({"key": "hi"})
        self.addCleanup(p.unregister)
        p.register()
        self.assertEqual(p.key, "demo.hi")
        self.assertEqual(get_pipeline_metadata_registry().get("demo.hi").description, "說明")
        self.assertIsNotNone(get_pipeline_control_panel_registry().get("demo.hi"))
        self.assertEqual(loaded, [])
        self.assertEqual(get_pipeline_run_registry().get("demo.hi")(), "ran")
        self.assertEqual(registered("demo"), ["hi"])
        p.unregister()
        self.assertEqual(registered("demo"), [])

    def test_bad_construction(self):
        m = PipelineMetadata(name="x")
        with self.assertRaises(ValueError):
            PipelinePlugin(stage="s", metadata=m)
        with self.assertRaises(ValueError):
            PipelinePlugin(stage="s", metadata=m, run=lambda: 1, load_run=lambda: lambda: 1)
        with self.assertRaises(ValueError):
            PipelinePlugin(stage="s", metadata=m, run=lambda: 1).register()          # 沒有 key


class PluginLayoutTest(unittest.TestCase):
    EXPECTED = {   # 原本 common/blocks.py 手寫表的順序（預設就是這個順序）
        "detect": ["dynamic_baseline", "peer_compare", "threshold_rule", "chronic_issue", "quality_check", "level_shift", "trace_latency"],
        "grouping": ["same_entity"], "severity": ["golden_signals", "p0"],
        "rca_sections": ["anomalies", "entries", "segments", "slowest", "logs", "changes", "topology", "conclusion"],
        "similar": ["fields", "vector"], "summary": ["llm"], "hints": ["segment_hints", "checklist"],
        "suggest": ["false_positive", "missed", "precision", "rule_upgrade", "from_text"],
    }

    def test_main_preset_registers_every_stage_in_original_order(self):
        for stage, names in self.EXPECTED.items():
            with self.subTest(stage=stage):
                self.assertEqual(list(ST.stage(stage).builtin), names)
                for n in names:
                    self.assertTrue(callable(ST.stage(stage).load(n)), n)

    def test_every_plugin_folder_has_the_superset_files(self):
        pkgs = sorted(p.name for p in PLUGINS.iterdir() if p.is_dir() and p.name.startswith("plugin_"))
        self.assertEqual(pkgs, ["plugin_detect", "plugin_grouping", "plugin_hints", "plugin_rca", "plugin_severity", "plugin_similar",
                                "plugin_suggest", "plugin_summary"])
        for pkg in pkgs:
            for folder in (p for p in (PLUGINS / pkg).iterdir() if p.is_dir() and not p.name.startswith("__")):
                with self.subTest(plugin=f"{pkg}/{folder.name}"):
                    for f in ("__init__.py", "control_panel.py", f"{folder.name}.py"):
                        self.assertTrue((folder / f).exists(), f)
                    mod = importlib.import_module(f"plugins.{pkg}.{folder.name}.{folder.name}")
                    self.assertTrue(callable(mod.run))

    def test_golden_signals_defaults_match_grade_options(self):
        o = GradeOptions()
        self.assertEqual(GoldenSignalsPipelinePlugin().control_panel.build_form_data(),
                         {"window": o.window, "min_coverage": o.min_coverage, "min_duration": o.min_duration,
                          "min_span_critical": o.min_span_critical, "min_points_per_class": o.min_points_per_class,
                          "deploy_grace": o.deploy_grace})

    def test_p0_typo_is_caught(self):
        panel = P0PipelinePlugin().control_panel
        self.assertEqual(panel.build_form_data({"p0_services": ["pay-api"]}), {"p0_services": ["pay-api"]})
        with self.assertRaises(ValueError):
            panel.build_form_data({"p0_service": ["pay-api"]})

    def test_blocks_listing_shows_parameters(self):
        text = "\n".join(ST.describe(load_scenario()))
        self.assertIn("window (NumberControl, 預設 15)", text)
        self.assertIn("p0_services (ListControl, 預設 [])", text)


if __name__ == "__main__":
    unittest.main()

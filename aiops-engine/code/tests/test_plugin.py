"""主程式＋plugin（core/plugin.py、docs/pdca/meta/2026-11-plugin-architecture.md）：選哪個、門面等價、三個內建各填了什麼、
detector 的子命令跟著 plugin、報告段清單跟著 plugin。不連 ES。"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from core import plugin
from detector import owner as O
from investigator.alerts import merge as AL
from investigator.rca import path_sections as PS
from investigator.rca import report as R

T0 = datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc)


def env(**kw):
    return mock.patch.dict(os.environ, {k: v for k, v in kw.items() if v is not None})


class MyPlugin(plugin.Plugin):
    """客製 plugin：繼承、只改幾個。"""
    name = "mine"
    entity_keys = ("host",)

    def source_profile(self):
        return {"owner": {"index": "x"}, "log": {}}

    def domain(self):
        return {"entity": "主機", "unit": "工單", "trace": "流程", "new_value_hint": "", "categories": [("other", "其他")],
                "fp_reasons": [("other", "其他")], "checked": ["x"]}


class Registry(unittest.TestCase):
    def tearDown(self):
        plugin.reset()

    def test_default_is_observability_otel(self):
        with env():
            os.environ.pop("AIOPS_PLUGIN", None)
            os.environ.pop("AIOPS_SOURCE", None)
            self.assertEqual(plugin.current().name, "observability")
            self.assertEqual(plugin.source_name(), "otel")
            self.assertEqual(plugin.entity_label(), "服務")
            self.assertEqual(plugin.current().schedule, "configs/schedule/schedule.yaml")

    def test_env_selects_and_switch_is_live(self):
        with env(AIOPS_PLUGIN="factory"):
            self.assertEqual((plugin.current().name, plugin.source_name(), plugin.unit_label()), ("factory", "factory", "lot"))
        with env(AIOPS_PLUGIN="metrics_only"):
            self.assertEqual(plugin.current().name, "metrics_only")
            self.assertEqual(plugin.current().sections, ("anomalies", "logs", "changes", "conclusion"))
        with env(AIOPS_PLUGIN="observability", AIOPS_SOURCE="apm"):
            self.assertEqual((plugin.source_name(), plugin.current().schedule), ("apm", "configs/apm/schedule.yaml"))

    def test_unknown_name_and_bad_path(self):
        for bad in ("mars", "no.such.module:Plugin", "tests.test_plugin:Nope", "tests.test_plugin:T0"):
            with env(AIOPS_PLUGIN=bad), self.assertRaises(ValueError, msg=bad):
                plugin.current()

    def test_custom_plugin_by_dotted_path(self):
        with env(AIOPS_PLUGIN="tests.test_plugin:MyPlugin"):
            self.assertEqual(plugin.current().name, "mine")
            self.assertEqual(plugin.entity_label(), "主機")
            self.assertEqual(AL.entity_of({"keys": {"host": "h1"}, "series": "s"}), ("service", "h1"))
            self.assertEqual(O.owner_of({"host": "h1"}, {("svc", "h1"): ("D1", "G1")}), ("D1", "G1"))
            self.assertEqual(plugin.current().chain(None, {}, [], [], T0, T0), plugin.Chain())
            self.assertIsNone(plugin.current().change_at(None, {}, "h1", T0, T0))

    def test_ingest_hook_default_is_lookup_error(self):
        """基底與 observability 沒有 ingest：LookupError（api 回 404）；factory／metrics_only 自己讀 ingest.yaml 再對照（api 不 import plugins）。"""
        for p in (MyPlugin(), plugin.load("observability")):
            with self.assertRaises(LookupError):
                p.ingest(None, "x.yaml", "ml", [], T0)
        with mock.patch("plugins.factory.ingest_rows", return_value=(1, 0)) as ir:
            self.assertEqual(plugin.load("metrics_only").ingest(None, "configs/factory/ingest.yaml", "ml", [{"a": 1}], T0), (1, 0))
        self.assertEqual(ir.call_args.args[2:], ("ml", [{"a": 1}], T0))
        self.assertIn("inputs", ir.call_args.args[1])                   # 設定檔由 plugin 讀（IngestConfig）


class Builtins(unittest.TestCase):
    """三個內建各填了什麼（plugins/README.md 的表）。"""

    def test_observability(self):
        p = plugin.load("observability")
        self.assertEqual(p.where_of({"method": "GET", "route": "/a"}), "GET /a")
        self.assertEqual(p.where_of({"entry.name": "GET /x"}), "GET /x")
        self.assertEqual(p.where_of({"route": "GET /api"}), "GET /api")        # APM 的 transaction.name
        self.assertIsNone(p.where_of({"recipe": "R"}))
        self.assertIn("service", p.entity_keys)
        self.assertNotIn("equipment", p.entity_keys)
        self.assertIn("latency", p.severity_classes)                     # 黃金訊號在 plugin，不在 investigator
        self.assertEqual(sorted(p.commands()), ["chronic", "peer", "quality", "trace-collect", "trace-detect", "trace-merge", "trace-train"])   # IT 專用的子命令由 plugin 掛
        self.assertIn("segments", p.sections)

    def test_factory_and_metrics_only(self):
        f, m = plugin.load("factory"), plugin.load("metrics_only")
        for p in (f, m):
            self.assertEqual(p.entity_keys, ("equipment",))
            self.assertEqual(p.where_of({"recipe": "REC-1", "method": "GET", "route": "/a"}), "REC-1")
            self.assertIn("fdc", p.critical_levels)
            self.assertIn("pressure", p.severity_classes)
            self.assertEqual(p.source_profile()["owner"]["index"], "eqp-master")
        self.assertEqual(list(f.commands()), ["ingest", "paths-merge"])
        self.assertEqual(list(m.commands()), ["ingest"])            # 只有指標：有 ingest、沒有履歷合成
        self.assertIn("segments", f.sections)
        self.assertNotIn("segments", m.sections)
        self.assertEqual(m.chain(None, {}, [], [], T0, T0), plugin.Chain())

    def test_factory_chain_and_change_read_cfg(self):
        """factory 的 chain／change_at 從 cfg 拿索引、看不到就用預設；真正的查詢在 chain.py／changes.py（test_factory_alerts 測）。"""
        p = plugin.load("factory")
        seen = {}

        class ES:
            def search(self, index, body):
                seen["index"], seen["body"] = index, body
                hit = {"@timestamp": "2026-11-04T00:30:00+00:00"} if index.startswith("chg") else {"trace_id": "L1", "spans": []}
                return {"hits": {"hits": [{"_source": hit}]}}
        dep = p.change_at(ES(), {"alerts": {"deploy_index": "chg-*"}}, "ETCH-01", T0 - timedelta(hours=1), T0)
        self.assertEqual((seen["index"], dep), ("chg-*", datetime(2026, 11, 4, 0, 30, tzinfo=timezone.utc)))
        self.assertEqual(seen["body"]["query"]["bool"]["filter"][0], {"term": {"equipment_id": "ETCH-01"}})
        self.assertEqual(seen["body"]["query"]["bool"]["filter"][2], {"term": {"event_type": "recipe_change"}})
        anoms = [{"_id": "a", "@timestamp": T0.isoformat(), "keys": {"equipment": "METRO-01", "lot": "L1"}}]
        ch = p.chain(ES(), {"indices": {"traces": "lot-paths-*"}, "alerts": {"paths_lookback": "2d"}}, anoms, [], T0 - timedelta(hours=1), T0)
        self.assertEqual(seen["index"], "lot-paths-*")
        self.assertEqual(seen["body"]["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"], (T0 - timedelta(days=2)).isoformat())
        self.assertEqual((ch.calls, ch.links), (set(), {}))   # 履歷裡沒有站 → 沒有前站


class DetectorCommands(unittest.TestCase):
    """plugin 多出來的子命令只在那個 plugin 下才有。"""

    def tearDown(self):
        plugin.reset()

    def _help(self):
        import contextlib
        import io

        from detector.__main__ import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--help"]), 0)             # typer：--help 回 0，不丟 SystemExit
        return buf.getvalue()

    def test_paths_merge_only_under_factory(self):
        with env(AIOPS_PLUGIN="observability"):
            self.assertNotIn("paths-merge", self._help())
        with env(AIOPS_PLUGIN="factory"):
            self.assertIn("paths-merge", self._help())
        with env(AIOPS_PLUGIN="metrics_only"):
            self.assertNotIn("paths-merge", self._help())


class ReportSections(unittest.TestCase):
    """報告段跟著 plugin；沒有 trace 時要 trace 的段寫「略過」、log 段照跑、不炸（docs/pdca/meta/2026-11-plugin-architecture.md）。"""

    def tearDown(self):
        plugin.reset()

    def _run(self, sections, *, requests=0, logs=()):
        from argparse import Namespace
        cfg = {"indices": {"anomalies": "aiops-anomalies", "traces": "lot-paths-*", "logs": "eqp-logs-*"},
               "changes": {"lookback": "12h", "query": {"term": {"event_type": "pm"}}}}
        anoms = [{"_id": "a1", "@timestamp": T0.isoformat(), "series": "fdc.pressure", "value": 12.0, "median": 9.0, "z": 6.0,
                  "status": "anomaly", "keys": {"equipment": "ETCH-01", "recipe": "REC-1"}}]
        stats = {"requests": requests, "errors": 0, "p50_ms": 1.0, "p95_ms": 2.0}
        with mock.patch.object(R.E, "ES", return_value="es"), mock.patch.object(R.E, "anomalies", return_value=anoms), \
             mock.patch.object(R.E, "entry_stats", return_value=stats), mock.patch.object(R.E, "sample_path_ids", return_value=[]), \
             mock.patch.object(PS, "spans_of", return_value=[]), \
             mock.patch.object(R.E, "problem_logs", side_effect=lambda es, ix, s, a, b: list(logs) if a == T0 else []), \
             mock.patch.object(R.E, "change_logs", return_value=[{"ts": T0.isoformat(), "service": "ETCH-01", "message": "PM done"}]), \
             mock.patch.object(R.E, "logs_of_paths", return_value=[]):
            return R.investigate_full(cfg, Namespace(entity="ETCH-01", where=None, start=T0.isoformat(), end=(T0 + timedelta(hours=1)).isoformat(),
                                                     at=None, window=None), sections)

    def test_metrics_only_sections(self):
        log = {"ts": T0.isoformat(), "service": "ETCH-01", "level": "alarm", "trace_id": None, "message": "ALID 2041 Chamber B pressure"}
        with env(AIOPS_PLUGIN="metrics_only"):
            md, f = self._run(None, logs=[log])
        self.assertIn("## 1. Metrics", md)
        self.assertIn("## 2. Log", md)
        self.assertIn("## 3. 事故前 12h 內的變更", md)
        self.assertIn("## 結論", md)
        self.assertNotIn("慢在哪一段", md)
        self.assertIn("ALID 2041", md)
        self.assertEqual(f["changes"][0]["message"], "PM done")
        self.assertIsNone(f["requests"])                      # 沒看 trace，不是「看了沒有」：cmd_alert 不會為它重跑
        self.assertEqual(f["anomalies"][0]["series"], "fdc.pressure")
        with env(AIOPS_PLUGIN="metrics_only"):                 # 什麼線索都沒有時，結論說清楚這個設定看不到哪一段
            md, _ = self._run(None)
        self.assertIn("這個設定沒有履歷", md)

    def test_factory_without_traces_skips_but_still_reads_logs(self):
        with env(AIOPS_PLUGIN="factory"):
            md, f = self._run(None, requests=0)
        self.assertIn("## 2. 履歷：這個機台收到的 lot", md)
        self.assertIn("## 3. 履歷：慢在哪一段", md)
        self.assertIn("（沒有履歷，略過）", md)
        self.assertIn("## 5. Log", md)                         # 以前沒 trace 就 return，log 看不到；現在照跑
        self.assertIn("## 6. 事故前 12h 內的變更", md)
        self.assertEqual(f["requests"]["requests"], 0)         # alerts/command.has_paths 看這個決定要不要重跑
        self.assertEqual(f["segments"], [])

    def test_unknown_section_rejected(self):
        with self.assertRaises(ValueError):
            self._run(("anomalies", "nope"))

"""情境（scenario）：環境變數選哪份、三個內建各填了什麼、客製怎麼給、detector 的子命令與報告段跟著情境。不連 ES。"""
import dataclasses
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import scenario.base as SC
from core.repository import Repos
from core.repository.owner import owner_of
from scenario import Chain, Scenario, load_scenario
import service.converge.merge as AL
import service.rca.report as R

T0 = datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc)
# 客製：拿內建的改幾個欄位（AIOPS_PLUGIN=tests.test_scenario:MINE）
MINE = dataclasses.replace(load_scenario("observability"), name="mine", entity="主機", unit="工單", entity_keys=("host",),
                           owner={"index": "x"}, chain=SC.NoChain(), change=SC.NoChange())


def env(**kw):
    return mock.patch.dict(os.environ, {k: v for k, v in kw.items() if v is not None})


class Loading(unittest.TestCase):
    def test_default_is_observability_otel(self):
        with env():
            os.environ.pop("AIOPS_PLUGIN", None)
            os.environ.pop("AIOPS_SOURCE", None)
            sc = load_scenario()
        self.assertEqual((sc.name, sc.source, sc.entity, sc.schedule), ("observability", "otel", "服務", "configs/schedule/schedule.yaml"))

    def test_env_selects(self):
        with env(AIOPS_PLUGIN="factory"):
            sc = load_scenario()
            self.assertEqual((sc.name, sc.source, sc.unit), ("factory", "factory", "lot"))
        with env(AIOPS_PLUGIN="metrics_only"):
            self.assertEqual(load_scenario().sections, ("anomalies", "logs", "changes", "conclusion"))
        with env(AIOPS_PLUGIN="observability", AIOPS_SOURCE="apm"):
            sc = load_scenario()
            self.assertEqual((sc.source, sc.schedule), ("apm", "configs/apm/schedule.yaml"))
            self.assertEqual(sc.owner["service"], "service.name")
            self.assertEqual(sc.entity_keys, load_scenario("observability").entity_keys)     # extends：沒改的欄位跟 it_otel 一樣

    def test_unknown_name_and_bad_path(self):
        for bad in ("mars", "no.such.module:X", "tests.test_scenario:Nope", "tests.test_scenario:T0"):
            with env(AIOPS_PLUGIN=bad), self.assertRaises(ValueError, msg=bad):
                load_scenario()
        with env(AIOPS_PLUGIN="observability", AIOPS_SOURCE="zipkin"), self.assertRaises(ValueError):
            load_scenario()

    def test_custom_by_dotted_path(self):
        with env(AIOPS_PLUGIN="tests.test_scenario:MINE"):
            sc = load_scenario()
            self.assertEqual((sc.name, sc.entity), ("mine", "主機"))
            self.assertEqual(AL.entity_of({"keys": {"host": "h1"}, "series": "s"}, sc), ("service", "h1"))
            self.assertEqual(owner_of({"host": "h1"}, {("svc", "h1"): ("D1", "G1")}, sc.entity_keys), ("D1", "G1"))
            self.assertEqual(sc.chain(None, {}, [], [], T0, T0), Chain())
            self.assertIsNone(sc.change(None, {}, "h1", T0, T0))


class Builtins(unittest.TestCase):
    """三個內建各填了什麼（scenario/README.md 的表）。"""

    def test_observability(self):
        sc = load_scenario("observability")
        self.assertEqual(sc.route_of({"method": "GET", "route": "/a"}), "GET /a")
        self.assertEqual(sc.route_of({"entry.name": "GET /x"}), "GET /x")
        self.assertEqual(sc.route_of({"route": "GET /api"}), "GET /api")        # APM 的 transaction.name
        self.assertIsNone(sc.route_of({"recipe": "R"}))
        self.assertIn("service", sc.entity_keys)
        self.assertNotIn("equipment", sc.entity_keys)
        self.assertIsNone(sc.severity_classes)
        self.assertEqual(sc.commands, {})
        self.assertIn("segments", sc.sections)
        self.assertEqual(sc.unit_text(), "請求")

    def test_factory_and_metrics_only(self):
        f, m = load_scenario("factory"), load_scenario("metrics_only")
        for sc in (f, m):
            self.assertEqual(sc.entity_keys, ("equipment",))
            self.assertEqual(sc.route_of({"recipe": "REC-1", "method": "GET", "route": "/a"}), "REC-1")
            self.assertIn("fdc", sc.critical_levels)
            self.assertIn("pressure", sc.severity_classes)
            self.assertEqual(sc.owner["index"], "eqp-master")
            self.assertEqual(sc.unit_text(), " lot ")
        self.assertEqual(list(f.commands), ["paths-merge"])
        self.assertEqual(m.commands, {})
        self.assertIn("segments", f.sections)
        self.assertNotIn("segments", m.sections)
        self.assertEqual(m.chain(None, {}, [], [], T0, T0), Chain())
        self.assertEqual(m.name, "metrics_only")

    def test_factory_chain_and_change_read_cfg(self):
        """factory 的 chain／change 從 cfg 拿索引、看不到就用預設；真正的查詢在 scenario/factory（test_factory_alerts 測）。"""
        sc = load_scenario("factory")
        seen = {}

        class ES:
            def search(self, index, body):
                seen["index"], seen["body"] = index, body
                hit = {"@timestamp": "2026-11-04T00:30:00+00:00"} if index.startswith("chg") else {"trace_id": "L1", "spans": []}
                return {"hits": {"hits": [{"_source": hit}]}}
        repo = Repos(ES(), sc)
        dep = sc.change(repo, {"alerts": {"deploy_index": "chg-*"}}, "ETCH-01", T0 - timedelta(hours=1), T0)
        self.assertEqual((seen["index"], dep), ("chg-*", datetime(2026, 11, 4, 0, 30, tzinfo=timezone.utc)))
        self.assertEqual(seen["body"]["query"]["bool"]["filter"][0], {"term": {"equipment_id": "ETCH-01"}})
        self.assertEqual(seen["body"]["query"]["bool"]["filter"][2], {"term": {"event_type": "recipe_change"}})
        anoms = [{"_id": "a", "@timestamp": T0.isoformat(), "keys": {"equipment": "METRO-01", "lot": "L1"}}]
        ch = sc.chain(repo, {"indices": {"traces": "lot-paths-*"}, "alerts": {"paths_lookback": "2d"}}, anoms, [], T0 - timedelta(hours=1), T0)
        self.assertEqual(seen["index"], "lot-paths-*")
        self.assertEqual(seen["body"]["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"], (T0 - timedelta(days=2)).isoformat())
        self.assertEqual((ch.calls, ch.links), (set(), {}))   # 履歷裡沒有站 → 沒有前站

    def test_scenario_is_a_frozen_value(self):
        sc = load_scenario("factory")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            sc.name = "x"
        self.assertIsInstance(sc, Scenario)


class DetectorCommands(unittest.TestCase):
    """情境多出來的子命令只在那個情境下才有。"""

    def _help(self):
        import contextlib
        import io

        from interface.cli.detect import main
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
    """報告段跟著情境；沒有 trace 時要 trace 的段寫「略過」、log 段照跑、不炸（docs/pdca/meta/2026-11-plugin-architecture.md）。"""

    def _run(self, sections, *, requests=0, logs=()):
        from argparse import Namespace
        cfg = {"indices": {"anomalies": "aiops-anomalies", "traces": "lot-paths-*", "logs": "eqp-logs-*"},
               "changes": {"lookback": "12h", "query": {"term": {"event_type": "pm"}}}}
        anoms = [{"_id": "a1", "@timestamp": T0.isoformat(), "series": "fdc.pressure", "value": 12.0, "median": 9.0, "z": 6.0,
                  "status": "anomaly", "keys": {"equipment": "ETCH-01", "recipe": "REC-1"}}]
        stats = {"requests": requests, "errors": 0, "p50_ms": 1.0, "p95_ms": 2.0}
        repo = mock.Mock()
        repo.scenario = load_scenario()
        repo.anomalies.between.return_value = anoms
        repo.traces.entry_stats.return_value = stats
        repo.traces.sample_trace_ids.return_value = []
        repo.traces.spans.return_value = []
        repo.logs.problems.side_effect = lambda ix, s, a, b: list(logs) if a == T0 else []
        repo.logs.changes.return_value = [{"ts": T0.isoformat(), "service": "ETCH-01", "message": "PM done"}]
        repo.logs.of_traces.return_value = []
        with mock.patch.object(R, "Repos", return_value=repo), mock.patch.object(R, "ES", return_value=None):
            return R.investigate_full(cfg, Namespace(service="ETCH-01", route=None, start=T0.isoformat(), end=(T0 + timedelta(hours=1)).isoformat(),
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
        self.assertIsNone(f["requests"])                      # 沒看 trace，不是「看了沒有」：alert 不會為它重跑
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
        self.assertEqual(f["requests"]["requests"], 0)         # alert_service.has_trace 看這個決定要不要重跑
        self.assertEqual(f["segments"], [])

    def test_unknown_section_rejected(self):
        with self.assertRaises(ValueError):
            self._run(("anomalies", "nope"))

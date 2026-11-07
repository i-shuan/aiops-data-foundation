"""工廠 plugin 的事件整合與嚴重度（docs/pdca/meta/2026-11-factory-engine.md）：對象是機台、API 是 recipe、前後站從履歷找、
嚴重度依參數家族、recipe 變更當換版。不連 ES。整個模組在 AIOPS_PLUGIN=factory 下跑（common/scenario/factory.yaml）。"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from common.scenario import load_scenario
import common.scenario.base as SC
import incident.alert_grouper as AL
import incident.alert_severity as S
import incident.strategy.fab_process_flow as FAC

T0 = datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc)
GAP = timedelta(hours=2)
_n = 0
_env = mock.patch.dict(os.environ, {"AIOPS_PLUGIN": "factory"})


def setUpModule():
    _env.start()


def tearDownModule():
    _env.stop()


def anom(eq, minute, *, series="fdc.pressure", lot="LOT-1", status="anomaly", severity="high", level="ml", recipe="REC-1", **extra):
    global _n
    _n += 1
    a = {"_id": f"f{_n}", "@timestamp": (T0 + timedelta(minutes=minute)).isoformat(), "series": series, "level": level, "status": status,
         "severity": severity, "keys": {"equipment": eq, "chamber": "CH-B", "recipe": recipe, "lot": lot, "parameter": series.split(".")[-1]},
         "value": 12.0, "median": 9.5, "z": 7}
    a.update(extra)
    return a


def lot_doc(lot, stations):
    """stations：[(機台, 開始分鐘, 長度分鐘), …]（相對 T0）。"""
    spans = [{"span_id": lot, "parent": None, "service": "lot", "name": "P", "kind": "Internal", "start_ms": T0.timestamp() * 1000, "dur_ms": 0}]
    for i, (eq, start, dur) in enumerate(stations, 1):
        spans.append({"span_id": f"{lot}/{i}", "parent": lot, "service": eq, "name": "REC-1", "kind": "Server",
                      "start_ms": (T0 + timedelta(minutes=start)).timestamp() * 1000, "dur_ms": dur * 60_000})
    return {"trace_id": lot, "spans": spans}


class Entity(unittest.TestCase):
    def test_equipment_is_the_entity_and_recipe_is_the_route(self):
        a = anom("ETCH-01", 0)
        self.assertEqual(AL.entity_of(a), ("service", "ETCH-01"))
        self.assertEqual(AL.route_of(a["keys"]), "REC-1")
        self.assertEqual(AL.kind_of(a), "open")
        self.assertEqual(AL.kind_of(anom("ETCH-01", 0, status="warning")), "attach")   # ML 的 weak 只併進開著的件

    def test_same_equipment_one_alert_with_lots(self):
        out = AL.merge([], [anom("ETCH-01", 0, lot="LOT-1"), anom("ETCH-01", 30, lot="LOT-2", series="fdc.rf_power")], T0 + GAP, GAP)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].service, out[0].lots, out[0].routes), ("ETCH-01", ["LOT-1", "LOT-2"], ["REC-1"]))


class LotLinks(unittest.TestCase):
    DOCS = {"LOT-1": lot_doc("LOT-1", [("LITHO-01", -60, 30), ("ETCH-01", -20, 50), ("CVD-01", 40, 45), ("METRO-01", 100, 15)])}

    def test_links_list_previous_stations_nearest_first(self):
        a = anom("METRO-01", 115, series="fdc.cd_bias")
        links = FAC.links_from_docs([a], self.DOCS)
        names = [n for n, _ in links[a["_id"]]]
        self.assertEqual(names, ["CVD-01", "ETCH-01", "LITHO-01"])
        at = dict(links[a["_id"]])["ETCH-01"]
        self.assertEqual(at, T0 + timedelta(minutes=30))                     # lot 離開 ETCH-01 的時間（出站）

    def test_no_link_without_lot_or_doc(self):
        self.assertEqual(FAC.links_from_docs([anom("METRO-01", 115, lot="LOT-X")], self.DOCS), {})
        a = anom("METRO-01", 115)
        del a["keys"]["lot"]
        self.assertEqual(FAC.links_from_docs([a], self.DOCS), {})
        first = anom("LITHO-01", -40)                                        # 第一站：前面沒有站
        self.assertEqual(FAC.links_from_docs([first], self.DOCS), {})

    def test_metrology_anomaly_joins_the_station_that_had_the_lot(self):
        """ETCH-01、CVD-01 都有開著的件、時間都對得上：lot 在 ETCH-01 就異常過（那件的 lots 有它）→ 併進 ETCH-01，不是最近的 CVD-01。"""
        etch = anom("ETCH-01", 30, lot="LOT-1")
        cvd = anom("CVD-01", 85, lot="LOT-9", series="fdc.temperature")
        metro = anom("METRO-01", 115, lot="LOT-1", series="fdc.cd_bias")
        links = FAC.links_from_docs([metro], self.DOCS)
        out = {al.service: al for al in AL.merge([], [etch, cvd, metro], T0 + timedelta(minutes=120), GAP, links=links)}
        self.assertEqual(set(out), {"ETCH-01", "CVD-01"})
        self.assertEqual(out["ETCH-01"].upstream_services, ["METRO-01"])
        self.assertIn(metro["_id"], out["ETCH-01"].anomaly_ids)
        self.assertEqual(out["CVD-01"].upstream_services, [])

    def test_falls_back_to_nearest_station_whose_alert_covers_the_time(self):
        """沒有哪件已經有這批 lot：取最近、而且 lot 在那站的時間落在那件範圍內的前站。"""
        cvd = anom("CVD-01", 85, lot="LOT-9", series="fdc.temperature")
        metro = anom("METRO-01", 115, lot="LOT-1", series="fdc.cd_bias")
        links = FAC.links_from_docs([metro], self.DOCS)
        out = {al.service: al for al in AL.merge([], [cvd, metro], T0 + timedelta(minutes=120), GAP, links=links)}
        self.assertEqual(list(out), ["CVD-01"])
        self.assertEqual(out["CVD-01"].upstream_services, ["METRO-01"])

    def test_own_open_alert_wins_over_links(self):
        metro_before = anom("METRO-01", 100, lot="LOT-7", series="fdc.cd_bias")
        etch = anom("ETCH-01", 30, lot="LOT-1")
        metro = anom("METRO-01", 115, lot="LOT-1", series="fdc.cd_bias")
        links = FAC.links_from_docs([metro], self.DOCS)
        out = {al.service: al for al in AL.merge([], [etch, metro_before, metro], T0 + timedelta(minutes=120), GAP, links=links)}
        self.assertEqual(out["METRO-01"].anomaly_ids, [metro_before["_id"], metro["_id"]])
        self.assertEqual(out["ETCH-01"].upstream_services, [])

    def test_time_outside_window_does_not_join(self):
        etch = anom("ETCH-01", -600, lot="LOT-5")                           # 10 小時前就結束的件
        metro = anom("METRO-01", 115, lot="LOT-1", series="fdc.cd_bias")
        links = FAC.links_from_docs([metro], self.DOCS)
        out = {al.service: al for al in AL.merge([], [etch, metro], T0 + timedelta(minutes=120), GAP, links=links)}
        self.assertIn("METRO-01", out)
        self.assertEqual(out["ETCH-01"].upstream_services, [])


class SeverityClasses(unittest.TestCase):
    CLASSES = S.parse_classes({"pressure": ["pressure", "vacuum"], "temperature": ["temp"], "metrology": ["cd", "overlay"], "alarm": ["alarm", "oos"]})

    def test_parse_and_classify(self):
        self.assertEqual(S.signal_class("fdc.pressure", self.CLASSES), "pressure")
        self.assertEqual(S.signal_class("fdc.temperature", self.CLASSES), "temperature")
        self.assertEqual(S.signal_class("fdc.cd_bias", self.CLASSES), "metrology")
        self.assertEqual(S.signal_class("fdc.flow", self.CLASSES), "other")
        self.assertEqual(S.signal_class("fdc.pressure"), "other")             # 內建的 IT 分類不認得 FDC 參數
        self.assertIsNone(S.parse_classes(None))
        self.assertEqual(S.parse_classes([{"name": "a", "patterns": ["x"]}]), (("a", ("x",)),))

    def test_two_families_is_high_three_sustained_is_critical(self):
        ts = [T0 + timedelta(minutes=m) for m in range(0, 60, 10)]
        g = S.grade(["fdc.pressure", "fdc.temperature"] * 3, ts, classes=self.CLASSES)
        self.assertEqual((g["severity"], g["breadth"]), ("high", 2))
        g = S.grade(["fdc.pressure", "fdc.temperature", "fdc.cd_bias"] * 2, ts, classes=self.CLASSES)
        self.assertEqual(g["severity"], "critical")

    def test_recipe_change_caps_to_warning(self):
        ts = [T0 + timedelta(minutes=m) for m in (3, 9, 14, 21, 25)]
        g = S.grade(["fdc.temperature", "fdc.pressure"] * 3, ts + [ts[0]], deploy_at=T0, opts=S.GradeOptions(deploy_grace=30), classes=self.CLASSES)
        self.assertEqual((g["severity"], g["capped"]), ("warning", True))
        self.assertIn("換版後", g["why"])


class Domain(unittest.TestCase):
    def test_factory_domain_tables(self):
        sc = load_scenario()
        self.assertEqual((sc.entity, sc.unit), ("機台", "lot"))
        self.assertIn("equipment", sc.category_codes())
        self.assertIn(("pm_just_done", "剛做完 PM，暖機中"), sc.fp_reasons)
        with mock.patch.dict(os.environ, {"AIOPS_PLUGIN": "observability"}):
            self.assertEqual(load_scenario().category_codes(), ("db", "network", "config_change", "deploy", "external", "resource", "bug", "data", "other"))
        with mock.patch.dict(os.environ, {"AIOPS_PLUGIN": "mars"}), self.assertRaises(ValueError):
            load_scenario()

    def test_every_builtin_scenario_has_other_category(self):
        for name in SC.BUILTIN:
            sc = load_scenario(name)
            self.assertEqual(sc.categories[-1][0], "other", name)
            self.assertEqual(sc.fp_reasons[-1][0], "other", name)
            self.assertTrue(sc.checked, name)

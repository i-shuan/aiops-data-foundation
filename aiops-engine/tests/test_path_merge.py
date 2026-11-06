"""工廠 plugin 的履歷合成器（plugins/factory/paths.py）：MES 進出站＋S6F11 → 跟 traces-merged 同格式的一筆。不連 ES。"""
import unittest
from datetime import datetime, timezone

import detect.store.models as T
import incident.rca_analyzer as A
import detect.trace.path_builder as P

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)
CFG = {"group": "fab1", "dept_field": "dept",
       "mes": {"index": "mes-*", "fields": {"lot": "lot_id", "step": "step_no", "station": "station", "equipment": "equipment_id", "chamber": "chamber_id",
                                             "recipe": "recipe_id", "recipe_version": "recipe_version", "product": "product_id",
                                             "start": "track_in", "end": "track_out", "result": "result"},
               "bad_results": ["hold", "scrap"], "attrs": ["material_lot"]},
       "events": {"index": "ev-*", "fields": {"lot": "lot_id", "equipment": "equipment_id", "chamber": "chamber_id", "step": "recipe_step",
                                               "start": "start_time", "end": "end_time", "event": "ceid"}, "attrs": ["wafer_count"]}}


def step(lot, no, eq, t_in, t_out, result="ok", chamber="CH-A", recipe="REC-1", rv="v1"):
    return {"lot_id": lot, "step_no": no, "station": eq.split("-")[0], "equipment_id": eq, "chamber_id": chamber, "recipe_id": recipe, "recipe_version": rv,
            "product_id": "P-100", "track_in": t_in, "track_out": t_out, "result": result, "material_lot": "MAT-3", "dept": "FAB1-ETCH"}


STEPS = [step("LOT-1", 1, "LITHO-01", "2026-11-04T00:00:00Z", "2026-11-04T00:30:00Z"),
         step("LOT-1", 2, "ETCH-01", "2026-11-04T00:40:00Z", "2026-11-04T01:30:00Z", chamber="CH-B"),
         step("LOT-1", 3, "METRO-01", "2026-11-04T01:40:00Z", "2026-11-04T01:55:00Z", result="hold")]
EVENTS = [{"lot_id": "LOT-1", "equipment_id": "ETCH-01", "chamber_id": "CH-B", "recipe_step": "MAIN", "ceid": "PROCESS",
           "start_time": "2026-11-04T00:43:00Z", "end_time": "2026-11-04T01:28:00Z", "wafer_count": 25},
          {"lot_id": "LOT-1", "equipment_id": "CVD-99", "chamber_id": "X", "recipe_step": "MAIN", "ceid": "PROCESS",     # 對不到站：丟掉
           "start_time": "2026-11-04T00:43:00Z", "end_time": "2026-11-04T00:50:00Z"}]


class BuildLot(unittest.TestCase):
    def test_root_steps_and_events(self):
        spans = P.build_lot("LOT-1", STEPS, EVENTS, CFG, NOW)
        root = T.root_of(spans)
        self.assertEqual((root["service"], root["name"], root["kind"], root["trace_id"]), ("lot", "P-100", "Internal", "LOT-1"))
        self.assertEqual(root["dur_ms"], 115 * 60_000)                    # 00:00 → 01:55
        steps = [s for s in spans if s["kind"] == "Server"]
        self.assertEqual([s["service"] for s in steps], ["LITHO-01", "ETCH-01", "METRO-01"])
        self.assertEqual([s["name"] for s in steps], ["REC-1"] * 3)        # 站的 name 是 recipe（根因分析用它挑 lot）
        self.assertEqual([s["parent"] for s in steps], ["LOT-1"] * 3)
        self.assertEqual([s["error"] for s in steps], [False, False, True])  # hold 算錯
        etch = steps[1]
        self.assertEqual(etch["attrs"]["chamber"], "CH-B")
        self.assertEqual((etch["resource"]["chamber"], etch["resource"]["dept"], etch["resource"]["group"]), ("CH-B", "FAB1-ETCH", "fab1"))
        self.assertEqual(etch["attrs"]["material_lot"], "MAT-3")
        ev = [s for s in spans if s["kind"] == "Internal" and s["parent"] == etch["span_id"]]
        self.assertEqual(len(ev), 1)                                        # CVD-99 的事件沒有站可掛
        self.assertEqual((ev[0]["name"], ev[0]["service"], ev[0]["attrs"]["event"], ev[0]["attrs"]["wafer_count"]), ("CH-B MAIN", "ETCH-01", "PROCESS", 25))
        self.assertEqual(ev[0]["dur_ms"], 45 * 60_000)

    def test_self_time_of_step_excludes_chamber_time(self):
        spans = P.build_lot("LOT-1", STEPS, EVENTS, CFG, NOW)
        st = T.self_times(spans)
        etch = next(s for s in spans if s["service"] == "ETCH-01" and s["kind"] == "Server")
        self.assertAlmostEqual(st[etch["span_id"]], 5 * 60_000)             # 50 分鐘的站、45 分鐘在 chamber 裡 → 自己 5 分鐘（搬運、等待）

    def test_open_step_uses_now(self):
        steps = [step("LOT-2", 1, "LITHO-01", "2026-11-04T05:00:00Z", None)]
        steps[0].pop("track_out")
        spans = P.build_lot("LOT-2", steps, [], CFG, NOW)
        s = [x for x in spans if x["kind"] == "Server"][0]
        self.assertEqual(s["dur_ms"], 60 * 60_000)
        self.assertTrue(s["attrs"]["in_progress"])

    def test_no_events_config(self):
        cfg = {k: v for k, v in CFG.items() if k != "events"}
        spans = P.build_lot("LOT-1", STEPS, EVENTS, cfg, NOW)
        self.assertEqual(len(spans), 4)                                     # root + 3 站，事件不掛

    def test_build_docs_same_shape_as_traces_merged(self):
        docs = P.build_docs(STEPS, EVENTS, CFG, NOW)
        (index, batch), = docs.items()
        self.assertEqual(index, "lot-paths-fab1-2026.11.04")
        (lot, doc), = batch
        self.assertEqual(lot, "LOT-1")
        self.assertEqual((doc["trace_id"], doc["group"], doc["dept"], doc["error"]), ("LOT-1", "fab1", "FAB1-ETCH", True))
        self.assertEqual(doc["services"], ["ETCH-01", "LITHO-01", "METRO-01", "lot"])
        entries = [(e["service"], e["name"], e["kind"]) for e in doc["entries"]]
        self.assertIn(("ETCH-01", "REC-1", "Server"), entries)              # 站是入口：依機台／recipe 挑 lot
        self.assertEqual(T.schema_of(index), "merged")                      # 讀回來走 merged 那條路
        back = T.from_merged(doc)
        self.assertEqual(len(back), doc["span_count"])
        segs = {T.segment(s) for s in back}
        self.assertIn("ETCH-01 › CH-B MAIN", segs)

    def test_bad_lot_vs_good_lot_compare_segments(self):
        """壞 lot 的 chamber 段比好 lot 多花 20 分鐘 → compare_segments 第一名是那一段。"""
        good = P.build_lot("LOT-G", STEPS, EVENTS, CFG, NOW)
        slow_steps = [dict(s) for s in STEPS]
        slow_steps[1]["track_out"] = "2026-11-04T01:50:00Z"
        slow_ev = [dict(EVENTS[0], end_time="2026-11-04T01:48:00Z")]
        bad = P.build_lot("LOT-B", slow_steps, slow_ev, CFG, NOW)
        diffs = A.compare_segments({"LOT-B": bad}, {"LOT-G": good})
        self.assertEqual(diffs[0].segment, "ETCH-01 › CH-B MAIN")
        self.assertAlmostEqual(diffs[0].delta_ms, 20 * 60_000)

    def test_template_matches_traces_merged_fields(self):
        props = P.PATHS_TEMPLATE["template"]["mappings"]["properties"]
        self.assertEqual(props["spans"]["type"], "nested")
        self.assertEqual(props["entries"]["type"], "nested")
        self.assertEqual(P.PATHS_TEMPLATE["index_patterns"], ["lot-paths-*"])

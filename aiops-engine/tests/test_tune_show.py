"""tune --show：印出某條序列、某個對象現在生效的值與來源（detector.cmd_baseline.cmd_tune、effective_lines）。
用假的 ES（aiops-tuning 裡放兩筆都符合的 override），不連 ES；設定檔的 adjustments、calendar 關掉，只看這裡給的。"""
import contextlib
import io
import unittest

from shared.infra.repos import Repos
from shared.scenario import load_scenario

SC = load_scenario()
from argparse import Namespace
from unittest import mock

import detect.detect_service as C

CFG = {"interval": "5m", "adjustments_file": None, "calendar_file": None,
       "status": {"confirm_points": 2, "confirm_span": "30m", "instant_z": 15}, "detect": {"z": 5},
       "overrides": [{"series": "http.latency_p95_s", "match": {"service": "^mes-"}, "z": 6}],
       "series": [{"name": "http.latency_p95_s", "index": "m", "by": ["service"], "value": {"avg": "v"}, "min_abs": 0.2},
                  {"name": "http.error_rate", "index": "m", "by": ["service"], "value": {"avg": "v"}, "confirm_points": 1}]}

TUNING = [{"_id": "t-report", "_source": {"kind": "override", "series": "http.latency_p95_s", "match": {"service": "^mes-report"}, "z": 7, "confirm_points": 3,
                                          "at": "2026-10-20T00:00:00Z"}}]


class FakeES:
    def __init__(self):
        self.ensured = []

    def ensure_index(self, name, body):
        self.ensured.append(name)

    def req(self, method, path, body=None, ok=None):
        if path.startswith("/aiops-tuning/_search"):
            return {"hits": {"hits": TUNING}}
        return {}


def show(series, match):
    out = io.StringIO()
    args = Namespace(list=False, remove=False, show=True, series=series, match=match, resolution=None, z=None, min_abs=None,
                     confirm_points=None, instant_z=None, reason=None, by=None)
    with mock.patch.object(C, "ES", FakeES), contextlib.redirect_stdout(out):
        rc = C.cmd_tune(CFG, args)
    return rc, out.getvalue()


class TuneShow(unittest.TestCase):
    def test_later_override_wins_and_each_line_names_its_source(self):
        rc, text = show("http.latency_p95_s", ["service=mes-report-api"])
        self.assertEqual(rc, 0)
        lines = {l.split(" = ")[0].strip(): l.strip() for l in text.splitlines() if " = " in l}
        self.assertIn("z = 7.0（來自 t-report：http.latency_p95_s service=^mes-report）", lines["z"])        # aiops-tuning 那筆蓋掉 series.yaml 的 6
        self.assertIn("confirm_points = 3（來自 t-report", lines["confirm_points"])
        self.assertIn("min_abs = 0.2（series.yaml 預設）", lines["min_abs"])                                   # 沒人調的：序列自己的設定
        self.assertIn("instant_z = 15.0（series.yaml 預設）", lines["instant_z"])                              # status 全域值
        self.assertIn("resolution = 自動", lines["resolution"])

    def test_object_only_hit_by_config_override(self):
        rc, text = show("http.latency_p95_s", ["service=mes-api"])
        self.assertIn("z = 6.0（來自 series.yaml overrides#1：http.latency_p95_s service=^mes-）", text)
        self.assertIn("confirm_points = 2（series.yaml 預設）", text)

    def test_values_match_what_detect_uses(self):
        # --show 的值跟 detect 的 score_params／status_params 同一個函式算出來，不會兩套
        from detect.strategy.dynamic_baseline import DetectOptions
        from detect.series_ops.manual_adjust import load_overrides
        ov = load_overrides(Repos(FakeES(), SC), CFG)
        spec, key = CFG["series"][0], {"service": "mes-report-api"}
        self.assertEqual(C.score_params(spec, key, DetectOptions(**CFG["detect"]), ov), (7.0, 0.2))
        self.assertEqual(C.status_params(CFG, spec, key, ov), (3, 15.0))
        _, text = show("http.latency_p95_s", ["service=mes-report-api"])
        self.assertIn("z = 7.0", text)
        self.assertIn("min_abs = 0.2", text)
        self.assertIn("confirm_points = 3", text)

    def test_series_own_default_shown(self):
        _, text = show("http.error_rate", ["service=pay"])
        self.assertIn("confirm_points = 1（series.yaml 預設）", text)
        self.assertIn("z = 5.0（series.yaml 預設）", text)

    def test_requires_series_and_match_and_known_series(self):
        self.assertEqual(show(None, ["service=x"])[0], 2)
        self.assertEqual(show("http.error_rate", [])[0], 2)
        rc, text = show("nope", ["service=x"])
        self.assertEqual(rc, 2)
        self.assertIn("沒有這條序列", text)


if __name__ == "__main__":
    unittest.main()

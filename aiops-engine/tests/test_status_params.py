"""依序列、依對象決定「連續幾次才算」「偏多少不用等」（detector.cmd_baseline.status_params、with_status）。"""
import unittest

import pandas as pd

from common.config import validate
from tests.config_factory import series_of
from detect.detect_service import status_params, with_status

CFG = validate({"status": {"confirm_points": 2, "confirm_span": "30m", "instant_z": 15}}, "series")


def frame(rows):
    """rows：(service, 第幾個 5 分鐘, 超標?, z)"""
    t0 = pd.Timestamp("2026-10-25T02:00:00Z")
    return pd.DataFrame([{"service": s, "ts": t0 + pd.Timedelta(minutes=5 * i), "value": 200.0 if hit else 100.0,
                          "median": 100.0, "anomaly": hit, "z": z} for s, i, hit, z in rows])


class StatusParams(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(status_params(CFG, None, {}), (2, 15.0))
        self.assertEqual(status_params(CFG, series_of({"name": "http.error_rate", "confirm_points": 1}), {"service": "a"}), (1, 15.0))
        ov = [{"series": "http.error_rate", "match": {"service": "^a$"}, "confirm_points": 3, "instant_z": 30}]
        self.assertEqual(status_params(CFG, series_of({"name": "http.error_rate", "confirm_points": 1}), {"service": "a"}, ov), (3, 30.0))
        self.assertEqual(status_params(CFG, series_of({"name": "http.error_rate", "confirm_points": 1}), {"service": "b"}, ov), (1, 15.0))

    def test_no_instant_z(self):
        self.assertEqual(status_params(validate({"status": {}}, "series"), None, {}), (2, None))


class WithStatus(unittest.TestCase):
    def test_per_series_confirm_points(self):
        start = pd.Timestamp("2026-10-25T02:00:00Z")
        f = frame([("a", 0, True, 6.0)])
        err = with_status(CFG, f, ["service"], "5m", start, series_of({"name": "http.error_rate", "confirm_points": 1}))
        p95 = with_status(CFG, f, ["service"], "5m", start, series_of({"name": "http.latency_p95_s"}))
        self.assertEqual(err["status"].tolist(), ["anomaly"])
        self.assertEqual(p95["status"].tolist(), ["warning"])

    def test_per_object_override_only_hits_that_service(self):
        start = pd.Timestamp("2026-10-25T02:00:00Z")
        f = frame([("pay", 0, True, 6.0), ("mes", 0, True, 6.0)])
        ov = [{"series": "http.latency_p95_s", "match": {"service": "^pay"}, "confirm_points": 1}]
        out = with_status(CFG, f, ["service"], "5m", start, series_of({"name": "http.latency_p95_s"}), ov).set_index("service")["status"]
        self.assertEqual(out["pay"], "anomaly")
        self.assertEqual(out["mes"], "warning")

    def test_instant_z_global(self):
        start = pd.Timestamp("2026-10-25T02:00:00Z")
        f = frame([("a", 0, True, 20.0), ("b", 0, True, 6.0)])
        out = with_status(CFG, f, ["service"], "5m", start, series_of({"name": "pod.cpu"})).set_index("service")["status"]
        self.assertEqual((out["a"], out["b"]), ("anomaly", "warning"))

    def test_window_before_start_dropped_but_counts(self):
        start = pd.Timestamp("2026-10-25T02:05:00Z")
        f = frame([("a", 0, True, 6.0), ("a", 1, True, 6.0)])
        out = with_status(CFG, f, ["service"], "5m", start, series_of({"name": "pod.cpu"}))
        self.assertEqual(len(out), 1)
        self.assertEqual(out["status"].tolist(), ["anomaly"])

    def test_empty(self):
        f = frame([]).reindex(columns=["service", "ts", "value", "median", "anomaly", "z"])
        self.assertTrue(with_status(CFG, f, ["service"], "5m", pd.Timestamp("2026-10-25T02:00:00Z")).empty)


if __name__ == "__main__":
    unittest.main()

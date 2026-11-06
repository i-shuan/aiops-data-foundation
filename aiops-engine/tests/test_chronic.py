"""慢性病清單（detector.chronic）與行事曆（adjust.calendar_adjustments）的計算。不需要 ES。"""
import datetime as dt
import unittest

import pandas as pd

import aiops.service.detect.adjustments as A
import aiops.service.detect.chronic as C

TZ = "Asia/Taipei"


def daily(rows):
    """rows：[(service, error_type, [每天的筆數…])]，從 10/1 起每天一列。"""
    out = []
    for svc, et, counts in rows:
        for i, n in enumerate(counts):
            out.append({"service": svc, "error_type": et, "day": dt.date(2026, 10, 1) + dt.timedelta(days=i), "count": n})
    return pd.DataFrame(out)


class FindChronic(unittest.TestCase):
    keys = ["service", "error_type"]

    def test_every_day_is_chronic(self):
        d = daily([("mes", "TimeoutException", [100] * 7), ("pay", "NPE", [100, 0, 0, 0, 0, 0, 100])])
        got = C.find_chronic(d, self.keys, min_days=6, min_per_day=20)
        self.assertEqual(got["service"].tolist(), ["mes"])
        self.assertEqual((got["days"][0], got["total"][0], got["per_day"][0]), (7, 700, 100))

    def test_six_of_seven_counts(self):
        # 週末量少那天沒出現也算
        d = daily([("mes", "X", [50, 50, 50, 50, 50, 5, 50])])
        self.assertEqual(len(C.find_chronic(d, self.keys, min_days=6, min_per_day=20)), 1)
        self.assertEqual(len(C.find_chronic(d, self.keys, min_days=7, min_per_day=20)), 0)

    def test_sporadic_small_counts_not_chronic(self):
        d = daily([("mes", "X", [3] * 7)])
        self.assertTrue(C.find_chronic(d, self.keys, min_days=6, min_per_day=20).empty)

    def test_ranking_days_then_total(self):
        d = daily([("a", "X", [30] * 6 + [0]), ("b", "X", [30] * 7), ("c", "X", [500] * 6 + [0])])
        got = C.find_chronic(d, self.keys, min_days=6, min_per_day=20)
        self.assertEqual(got["service"].tolist(), ["b", "c", "a"])

    def test_top(self):
        d = daily([(f"s{i}", "X", [100 + i] * 7) for i in range(15)])
        self.assertEqual(len(C.find_chronic(d, self.keys, top=10)), 10)

    def test_missing_error_type_is_kept(self):
        d = daily([("mes", None, [100] * 7)])
        got = C.find_chronic(d, self.keys, min_per_day=20)
        self.assertEqual(len(got), 1)

    def test_empty(self):
        self.assertTrue(C.find_chronic(pd.DataFrame(columns=self.keys + ["day", "count"]), self.keys).empty)


class Owner(unittest.TestCase):
    owners = [{"match": {"service": "^mes-"}, "owner": "MES 團隊"}, {"match": {"namespace": "^pay"}, "owner": "金流"}]

    def test_alias_and_order(self):
        self.assertEqual(C.owner_of({"resource.attributes.service.name": "mes-api"}, self.owners), "MES 團隊")
        self.assertEqual(C.owner_of({"resource.attributes.k8s.namespace.name": "pay-prod"}, self.owners), "金流")
        self.assertIsNone(C.owner_of({"service": "other"}, self.owners))


class Calendar(unittest.TestCase):
    def test_whole_day_local_time(self):
        a, = A.calendar_adjustments([{"date": "2026-10-10", "name": "國慶日"}], TZ)
        self.assertEqual(a["kind"], "exclude_window")
        self.assertEqual(pd.Timestamp(a["start"]), pd.Timestamp("2026-10-10T00:00:00+08:00"))
        self.assertEqual(pd.Timestamp(a["end"]), pd.Timestamp("2026-10-11T00:00:00+08:00"))
        self.assertEqual(a["apply_to"], ["train", "detect"])   # 預設不學也不報

    def test_range_inclusive_and_time(self):
        a, b = A.calendar_adjustments([{"start": "2027-02-05", "end": "2027-02-07", "name": "春節"},
                                       {"start": "2026-11-01T02:00", "end": "2026-11-01T06:00", "name": "維護",
                                        "type": "maintenance"}], TZ)
        self.assertEqual(pd.Timestamp(a["end"]), pd.Timestamp("2027-02-08T00:00:00+08:00"))
        self.assertEqual(pd.Timestamp(b["start"]), pd.Timestamp("2026-11-01T02:00:00+08:00"))
        self.assertEqual(pd.Timestamp(b["end"]), pd.Timestamp("2026-11-01T06:00:00+08:00"))

    def test_detect_true_only_excludes_training(self):
        a, = A.calendar_adjustments([{"date": "2026-11-11", "name": "活動", "detect": True}], TZ)
        self.assertEqual(a["apply_to"], ["train"])

    def test_needs_dates(self):
        with self.assertRaises(ValueError):
            A.calendar_adjustments([{"name": "沒有日期"}], TZ)

    def test_applies_to_train_and_detect(self):
        adj = A.calendar_adjustments([{"date": "2026-10-10", "name": "國慶日", "match": {"service": "^mes"}}], TZ)
        ts = pd.date_range(pd.Timestamp("2026-10-09T23:00", tz=TZ), periods=3, freq="1h").tz_convert("UTC")
        hist = pd.DataFrame({"service": ["mes-api"] * 3, "ts": ts, "value": 1.0})
        out, n = A.train_filter(hist, ["service"], "http.requests", adj, TZ)
        self.assertEqual(n, 2)   # 10/10 00:00、01:00；10/9 23:00 不算
        scored = hist.assign(median=0.0, z=9.0, anomaly=True)
        self.assertEqual(A.detect_filter(scored, ["service"], "x", adj, TZ)[0]["anomaly"].tolist(), [True, False, False])


if __name__ == "__main__":
    unittest.main()

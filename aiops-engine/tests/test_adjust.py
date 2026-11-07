"""調整（detector.adjust）：排除時段、重置點、週期放寬、別名、生效條件。不需要 ES。"""
from common.config import validate
import unittest

import numpy as np
import pandas as pd

import detect.series_ops.manual_adjust as A
from common.infra.repos import Repos
from common.scenario import load_scenario

SC = load_scenario()
from detect.strategy.dynamic_baseline import override_for

TZ = "Asia/Taipei"


def series(keys: dict, start: str, periods: int, freq: str = "1h", value: float = 1.0) -> pd.DataFrame:
    ts = pd.date_range(pd.Timestamp(start, tz=TZ), periods=periods, freq=freq).tz_convert("UTC")
    return pd.DataFrame({**{k: [v] * periods for k, v in keys.items()}, "ts": ts, "value": value})


class Aliases(unittest.TestCase):
    def test_service_alias_matches_every_naming(self):
        for key in ({"service": "mes-api"}, {"resource.attributes.service.name": "mes-api"}, {"entry.service": "mes-api"}):
            self.assertTrue(A.match_keys(key, {"service": "^mes-api$"}), key)

    def test_no_such_field_does_not_match(self):
        self.assertFalse(A.match_keys({"resource.attributes.k8s.deployment.name": "x"}, {"service": "x"}))

    def test_empty_match_is_everything(self):
        self.assertTrue(A.match_keys({"service": "a"}, None))

    def test_override_for_uses_alias_and_ignores_other_kinds(self):
        overrides = [{"series": "log.lines", "match": {"service": "^a$"}, "z": 7},
                     {"kind": "exclude_window", "series": "log.lines", "start": "2026-01-01", "end": "2026-01-02"}]
        self.assertEqual(override_for("log.lines", {"resource.attributes.service.name": "a"}, overrides), {"z": 7})


class Active(unittest.TestCase):
    now = pd.Timestamp("2026-10-23T00:00:00Z")

    def test_status_and_expiry(self):
        adjs = [{"kind": "reset_point", "id": "a"},
                {"kind": "reset_point", "id": "proposed", "status": "proposed"},
                {"kind": "reset_point", "id": "expired", "expires_at": "2026-10-01T00:00:00Z"},
                {"kind": "reset_point", "id": "later", "expires_at": "2026-12-01T00:00:00Z"},
                {"id": "tune"}]   # 沒有 kind 是 override
        got = [a["id"] for a in A.active(adjs, self.now, kinds=("reset_point",))]
        self.assertEqual(got, ["a", "later"])
        self.assertEqual([a["id"] for a in A.active(adjs, self.now, kinds=("override",))], ["tune"])


class Robust(unittest.TestCase):
    """aiops-tuning 讀不到、或裡面有壞掉的調整：略過，不讓整個 train／detect 失敗。"""

    def test_problem(self):
        self.assertIsNone(A.problem({"kind": "exclude_window", "start": "2026-10-01", "end": "2026-10-02"}))
        self.assertIn("end", A.problem({"kind": "exclude_window", "start": "2026-10-01"}))
        self.assertIn("早於", A.problem({"kind": "exclude_window", "start": "2026-10-02", "end": "2026-10-01"}))
        self.assertIn("position", A.problem({"kind": "override", "series": "http.(requests", "z": 7}))
        self.assertIn("position", A.problem({"kind": "override", "match": {"service": "["}, "z": 7}))
        self.assertIn("reset_at", A.problem({"kind": "reset_point"}))
        self.assertIn("cron", A.problem({"kind": "recurring_override", "schedule": {"cron": "0 20 * *"}}))
        self.assertIn("ValueError", A.problem({"kind": "override", "z": "abc"}))
        self.assertIn("粒度", A.problem({"kind": "override", "resolution": "2m"}))
        self.assertIsNone(A.problem({"kind": "rule_suggestion", "rule_draft": {}}))

    def test_load_skips_bad_and_survives_unreadable_index(self):
        class FakeES:
            def __init__(self, hits=None, err=None):
                self.hits, self.err = hits, err

            def req(self, *a, **k):
                if self.err:
                    raise self.err
                return {"hits": {"hits": self.hits}}
        good = {"_id": "g", "_source": {"kind": "override", "series": "x", "z": 7}}
        bad = {"_id": "b", "_source": {"kind": "exclude_window", "start": "2026-10-01"}}
        cfg = validate({"adjustments_file": "", "calendar_file": "", "overrides": [{"series": "y", "z": 9}], "series": []}, "series")
        self.assertEqual([a.get("id") for a in A.load(Repos(FakeES([good, bad])), cfg)], [None, "g"])
        got = A.load(Repos(FakeES(err=RuntimeError("400 index_closed_exception"))), cfg)
        self.assertEqual(got, cfg["overrides"])          # 只剩設定檔的
        import requests
        self.assertEqual(A.load(Repos(FakeES(err=requests.ConnectionError("down"))), cfg), cfg["overrides"])


class LocalTime(unittest.TestCase):
    """人寫的時間沒寫時區 → 當地時間（跟行事曆一樣）；以前 CLI、adjustments.yaml 的當成 UTC，差 8 小時。"""

    def test_naive_is_local(self):
        self.assertEqual(A.local_iso("2026-09-21T02:00", TZ), "2026-09-21T02:00:00+08:00")
        self.assertEqual(A.local_iso("2026-09-21T02:00:00Z", TZ), "2026-09-21T02:00:00+00:00")
        self.assertIsNone(A.local_iso(None, TZ))

    def test_naive_window_excludes_local_hours(self):
        a = A.localize({"kind": "exclude_window", "start": "2026-09-21T02:00", "end": "2026-09-21T05:00"}, TZ)
        h = series({"service": "a"}, "2026-09-21T00:00", 24)
        kept, dropped = A.train_filter(h, ["service"], "s", [a], TZ)
        self.assertEqual(dropped, 3)
        self.assertEqual(sorted(set(range(24)) - set(kept["ts"].dt.tz_convert(TZ).dt.hour)), [2, 3, 4])

    def test_adjustments_file_naive_times_are_local(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "adj.yaml")
            with open(p, "w", encoding="utf-8") as f:
                f.write("adjustments:\n  - {id: x, kind: reset_point, reset_at: '2026-09-25T10:00'}\n")

            class NoES:
                def req(self, *a, **k):
                    raise RuntimeError("no es")
            got = A.load(Repos(NoES(), SC), validate({"adjustments_file": p, "calendar_file": None, "series": []}, "series"))
        self.assertEqual(pd.Timestamp(got[0]["reset_at"]), pd.Timestamp("2026-09-25T02:00:00Z"))


class Schedule(unittest.TestCase):
    def test_daily_window(self):
        ts = pd.Series(pd.to_datetime(["2026-10-23T19:59", "2026-10-23T20:00", "2026-10-23T23:59",
                                       "2026-10-24T00:00"]).tz_localize(TZ))
        got = A.in_schedule(ts, {"cron": "0 20 * * *", "duration": "4h"}, TZ)
        self.assertEqual(got.tolist(), [False, True, True, False])

    def test_last_day_of_month(self):
        ts = pd.Series(pd.to_datetime(["2026-10-30T21:00", "2026-10-31T21:00", "2026-02-28T21:00",
                                       "2026-11-30T21:00"]).tz_localize(TZ))
        got = A.in_schedule(ts, {"cron": "0 20 * * *", "duration": "4h", "days_of_month": [-1]}, TZ)
        self.assertEqual(got.tolist(), [False, True, True, True])

    def test_crosses_midnight(self):
        # 每月最後一天 22:00 開始、跑 4 小時：隔天 01:00 還在時段裡
        ts = pd.Series(pd.to_datetime(["2026-11-01T01:00", "2026-11-01T02:00"]).tz_localize(TZ))
        got = A.in_schedule(ts, {"cron": "0 22 * * *", "duration": "4h", "days_of_month": [-1]}, TZ)
        self.assertEqual(got.tolist(), [True, False])

    def test_weekday_and_step(self):
        # 週一到週五，每 15 分鐘開始一次、跑 5 分鐘
        ts = pd.Series(pd.to_datetime(["2026-10-23T09:16", "2026-10-23T09:21", "2026-10-24T09:16"]).tz_localize(TZ))
        got = A.in_schedule(ts, {"cron": "*/15 * * * 1-5", "duration": "5m"}, TZ)   # 10/23 週五、10/24 週六
        self.assertEqual(got.tolist(), [True, False, False])

    def test_bad_cron(self):
        with self.assertRaises(ValueError):
            A.in_schedule(pd.Series(pd.to_datetime(["2026-10-23"]).tz_localize(TZ)), {"cron": "0 20 *"}, TZ)


class TrainFilter(unittest.TestCase):
    keys = ["service"]

    def hist(self):
        return pd.concat([series({"service": "a"}, "2026-10-20T00:00", 72),
                          series({"service": "b"}, "2026-10-20T00:00", 72)], ignore_index=True)

    def test_exclude_window_only_matching_service(self):
        adj = {"kind": "exclude_window", "match": {"service": "^a$"},
               "start": "2026-10-21T00:00:00+08:00", "end": "2026-10-21T06:00:00+08:00"}
        out, n = A.train_filter(self.hist(), self.keys, "http.requests", [adj], TZ)
        self.assertEqual(n, 6)
        self.assertEqual(len(out[out["service"] == "b"]), 72)

    def test_exclude_window_detect_only_not_applied_to_train(self):
        adj = {"kind": "exclude_window", "apply_to": ["detect"], "start": "2026-10-21", "end": "2026-10-22"}
        self.assertEqual(A.train_filter(self.hist(), self.keys, "x", [adj], TZ)[1], 0)

    def test_series_regex(self):
        adj = {"kind": "exclude_window", "series": "log\\..*", "start": "2026-10-19", "end": "2026-10-25"}
        self.assertEqual(A.train_filter(self.hist(), self.keys, "http.requests", [adj], TZ)[1], 0)
        self.assertEqual(A.train_filter(self.hist(), self.keys, "log.lines", [adj], TZ)[1], 144)

    def test_reset_point_drops_older_history(self):
        adj = {"kind": "reset_point", "match": {"service": "a"}, "reset_at": "2026-10-22T00:00:00+08:00"}
        out, n = A.train_filter(self.hist(), self.keys, "x", [adj], TZ)
        a = out[out["service"] == "a"]
        self.assertEqual(n, 48)
        self.assertTrue((a["ts"] >= pd.Timestamp("2026-10-22T00:00:00+08:00")).all())

    def test_recurring_train_exclude(self):
        adj = {"kind": "recurring_override", "schedule": {"cron": "0 20 * * *", "duration": "2h"},
               "effect": {"z": 8, "train": "exclude"}}
        self.assertEqual(A.train_filter(self.hist(), self.keys, "x", [adj], TZ)[1], 2 * 3 * 2)   # 2 服務 × 3 天 × 2 小時
        keep = {**adj, "effect": {"z": 8}}
        self.assertEqual(A.train_filter(self.hist(), self.keys, "x", [keep], TZ)[1], 0)   # 預設照學


class DetectFilter(unittest.TestCase):
    keys = ["service"]

    def scored(self, at="2026-10-31T21:00", z=6.0, value=10.0, median=1.0, service="a"):
        return pd.DataFrame({"service": [service], "ts": [pd.Timestamp(at, tz=TZ).tz_convert("UTC")],
                             "value": [value], "median": [median], "z": [z], "anomaly": [True]})

    month_end = {"kind": "recurring_override", "schedule": {"cron": "0 20 * * *", "duration": "4h", "days_of_month": [-1]}}

    def test_mute(self):
        out, n = A.detect_filter(self.scored(), self.keys, "x", [{**self.month_end, "effect": {"mute": True}}], TZ)
        self.assertEqual((n, out["anomaly"].tolist()), (1, [False]))

    def test_relaxed_z(self):
        adj = {**self.month_end, "effect": {"z": 8}}
        self.assertFalse(A.detect_filter(self.scored(z=6), self.keys, "x", [adj], TZ)[0]["anomaly"][0])
        self.assertTrue(A.detect_filter(self.scored(z=9), self.keys, "x", [adj], TZ)[0]["anomaly"][0])
        self.assertTrue(A.detect_filter(self.scored(z=-9), self.keys, "x", [adj], TZ)[0]["anomaly"][0])

    def test_relaxed_min_abs(self):
        adj = {**self.month_end, "effect": {"min_abs": 20}}
        self.assertFalse(A.detect_filter(self.scored(value=10, median=1), self.keys, "x", [adj], TZ)[0]["anomaly"][0])

    def test_outside_schedule_unchanged(self):
        out, n = A.detect_filter(self.scored(at="2026-10-30T21:00"), self.keys, "x",
                                 [{**self.month_end, "effect": {"mute": True}}], TZ)
        self.assertEqual((n, out["anomaly"].tolist()), (0, [True]))

    def test_exclude_window_detect(self):
        adj = {"kind": "exclude_window", "apply_to": ["train", "detect"],
               "start": "2026-10-31T20:00:00+08:00", "end": "2026-10-31T22:00:00+08:00"}
        self.assertFalse(A.detect_filter(self.scored(), self.keys, "x", [adj], TZ)[0]["anomaly"][0])
        self.assertTrue(A.detect_filter(self.scored(), self.keys, "x", [{**adj, "apply_to": ["train"]}], TZ)[0]["anomaly"][0])

    def test_reset_point_warmup(self):
        adj = {"kind": "reset_point", "match": {"service": "a"}, "reset_at": "2026-10-31T00:00:00+08:00"}
        new = pd.DataFrame({"service": ["a"], "trained_at": ["2026-10-31T10:00:00Z"]})   # 重置後重新學過
        self.assertFalse(A.detect_filter(self.scored(at="2026-10-31T21:00"), self.keys, "x", [adj], TZ,
                                         baseline=new)[0]["anomaly"][0])   # 21 小時：還在預設 24 小時暖機
        self.assertTrue(A.detect_filter(self.scored(at="2026-11-01T01:00"), self.keys, "x", [adj], TZ,
                                        baseline=new)[0]["anomaly"][0])    # 25 小時：暖機過了
        short = {**adj, "warmup": "1h"}
        self.assertTrue(A.detect_filter(self.scored(at="2026-10-31T21:00"), self.keys, "x", [short], TZ,
                                        baseline=new)[0]["anomaly"][0])
        # 沒有 baseline 資料（例如借來的）也照暖機
        self.assertFalse(A.detect_filter(self.scored(at="2026-10-31T21:00"), self.keys, "x", [adj], TZ)[0]["anomaly"][0])

    def test_reset_point_stale_baseline_not_judged(self):
        adj = {"kind": "reset_point", "match": {"service": "a"}, "reset_at": "2026-10-31T00:00:00+08:00", "warmup": "1h"}
        old = pd.DataFrame({"service": ["a"], "trained_at": ["2026-10-30T03:15:00Z"]})
        new = pd.DataFrame({"service": ["a"], "trained_at": ["2026-11-02T03:15:00Z"]})
        self.assertFalse(A.detect_filter(self.scored(), self.keys, "x", [adj], TZ, baseline=old)[0]["anomaly"][0])
        self.assertTrue(A.detect_filter(self.scored(), self.keys, "x", [adj], TZ, baseline=new)[0]["anomaly"][0])
        # 事後補標：reset_at 在過去，baseline 在 reset_at 之後、但在這筆寫進來之前 train 的，還是舊的
        late = {**adj, "at": "2026-11-03T00:00:00Z"}
        self.assertFalse(A.detect_filter(self.scored(), self.keys, "x", [late], TZ, baseline=new)[0]["anomaly"][0])
        # 別的服務不受影響
        self.assertTrue(A.detect_filter(self.scored(service="b"), self.keys, "x", [adj], TZ,
                                        baseline=old.assign(service="b"))[0]["anomaly"][0])

    def test_only_removes_never_adds(self):
        s = self.scored().assign(anomaly=[False])
        out, n = A.detect_filter(s, self.keys, "x", [{**self.month_end, "effect": {"z": 1}}], TZ)
        self.assertEqual((n, out["anomaly"].tolist()), (0, [False]))

    def test_poisson_without_z(self):
        # 卜瓦松的結果 z 是 NaN：z 放寬不適用，mute、min_abs 照樣有效
        s = self.scored(z=np.nan)
        self.assertTrue(A.detect_filter(s, self.keys, "x", [{**self.month_end, "effect": {"z": 8}}], TZ)[0]["anomaly"][0])
        self.assertFalse(A.detect_filter(s, self.keys, "x", [{**self.month_end, "effect": {"mute": True}}], TZ)[0]["anomaly"][0])


if __name__ == "__main__":
    unittest.main()

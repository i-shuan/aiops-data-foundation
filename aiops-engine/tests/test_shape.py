"""tests/shaping.py（記憶體版來源的整形）：原始列 → 標準輸入，語意要跟 ES 端的 aggregation 一樣（docs/pdca/meta/2026-11-model-modules.md）。不連 ES。"""
import unittest

import numpy as np
import pandas as pd

import tests.shaping as S

T0 = pd.Timestamp("2026-11-02T00:00:00Z")


def rows(n_per_min=4, minutes=10, fail_every=4):
    """一列一個請求：兩個服務、兩條路由；每 fail_every 個請求一個失敗；耗時 100ms 起跳（微秒）。"""
    out = []
    i = 0
    for m in range(minutes):
        for svc, route in (("mes-api", "GET /lots"), ("mes-api", "POST /lots"), ("order-svc", "GET /orders")):
            for k in range(n_per_min):
                out.append({"@timestamp": (T0 + pd.Timedelta(minutes=m, seconds=k)).isoformat(), "service.name": svc, "transaction.name": route,
                            "event.outcome": "failure" if i % fail_every == 0 else "success", "transaction.duration.us": 100_000 + 10_000 * k,
                            "metricset.name": "transaction"})
                i += 1
    return pd.DataFrame(out)


class FilterTest(unittest.TestCase):
    df = pd.DataFrame({"a": ["x", "y", None], "n": [1, 5, 10], "t": ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"]})

    def test_term_terms_exists_range_bool(self):
        m = S.mask
        self.assertEqual(m(self.df, {"term": {"a": "x"}}).tolist(), [True, False, False])
        self.assertEqual(m(self.df, {"terms": {"a": ["x", "y"]}}).tolist(), [True, True, False])
        self.assertEqual(m(self.df, {"exists": {"field": "a"}}).tolist(), [True, True, False])
        self.assertEqual(m(self.df, {"range": {"n": {"gte": 5}}}).tolist(), [False, True, True])
        self.assertEqual(m(self.df, {"range": {"t": {"lt": "2026-01-02T12:00:00Z"}}}).tolist(), [True, True, False])
        self.assertEqual(m(self.df, {"bool": {"filter": [{"range": {"n": {"gt": 1}}}], "must_not": {"term": {"a": "y"}}}}).tolist(),
                         [False, False, True])
        self.assertEqual(m(self.df, {"bool": {"should": [{"term": {"a": "x"}}, {"term": {"n": 10}}]}}).tolist(), [True, False, True])
        self.assertEqual(m(self.df, {"term": {"nope": 1}}).tolist(), [False, False, False])   # 沒有的欄位不符合

    def test_unsupported_query_says_so(self):
        with self.assertRaises(S.ShapeError):
            S.mask(self.df, {"query_string": {"query": "a:x"}})


class SeriesFromEventsTest(unittest.TestCase):
    BY = ["service.name", "transaction.name"]
    KEYS = ["service", "route"]
    END = T0 + pd.Timedelta(minutes=10)

    def spec(self, **kw):
        return {"name": "t", "index": "x", "by": self.BY, "keys": self.KEYS, "filter": {"term": {"metricset.name": "transaction"}}, **kw}

    def test_count_per_minute(self):
        df = S.series_from_rows(rows(), self.spec(value="count"), T0, self.END, "1m")
        self.assertEqual(list(df.columns), self.KEYS + ["ts", "value", "n"])
        self.assertEqual(len(df), 30)                       # 3 條序列 × 10 分鐘
        self.assertTrue((df["value"] == 4).all() and (df["n"] == 4).all())

    def test_ratio_and_n(self):
        df = S.series_from_rows(rows(), self.spec(value={"ratio": {"term": {"event.outcome": "failure"}}}), T0, self.END, "5m")
        self.assertEqual(len(df), 6)
        self.assertTrue(np.allclose(df["value"], 0.25) and (df["n"] == 20).all())

    def test_percentile_with_scale_and_whole_window(self):
        df = S.series_from_rows(rows(), self.spec(value={"percentile": {"field": "transaction.duration.us", "percent": 95}}, scale=1e-6),
                                T0, self.END, None)
        self.assertEqual(len(df), 3)
        self.assertTrue((df["ts"] == self.END).all())      # interval None：整段一個值，ts 是 end
        self.assertTrue(np.allclose(df["value"], np.percentile([100_000, 110_000, 120_000, 130_000] * 10, 95) * 1e-6))
        self.assertTrue((df["n"] == 40).all())

    def test_avg_is_gauge_without_n(self):
        df = S.series_from_rows(rows(), self.spec(value={"avg": "transaction.duration.us"}, scale=1e-6), T0, self.END, "10m")
        self.assertTrue(np.allclose(df["value"], 0.115) and df["n"].isna().all())

    def test_div(self):
        r = pd.DataFrame({"@timestamp": [T0.isoformat()] * 3, "svc": ["a"] * 3, "cnt": [10, 20, 30], "sum_us": [1000, 4000, 9000]})
        spec = {"name": "d", "index": "x", "by": ["svc"], "value": {"div": {"num": "sum_us", "den": "cnt"}}, "scale": 1e-6}
        df = S.series_from_rows(r, spec, T0, self.END, "1m")
        self.assertAlmostEqual(df["value"][0], 14000 / 60 * 1e-6)
        self.assertEqual(df["n"][0], 60)

    def test_count_fills_zero_only_while_index_alive(self):
        r = rows(minutes=3)
        r = r[~((r["service.name"] == "order-svc") & (r["@timestamp"] >= (T0 + pd.Timedelta(minutes=1)).isoformat()))]   # order-svc 第 1 分鐘起沒流量
        df = S.series_from_rows(r, self.spec(value="count"), T0, T0 + pd.Timedelta(minutes=3), "1m")
        o = df[df["service"] == "order-svc"].sort_values("ts")
        self.assertEqual(o["value"].tolist(), [4.0, 0.0, 0.0])          # 別的服務還有資料 → 補 0
        r2 = r[r["@timestamp"] < (T0 + pd.Timedelta(minutes=2)).isoformat()]                        # 整個 index 第 2 分鐘沒資料 → 不知道
        df = S.series_from_rows(r2, self.spec(value="count"), T0, T0 + pd.Timedelta(minutes=3), "1m")
        o = df[df["service"] == "order-svc"].sort_values("ts")
        self.assertEqual(o["value"].tolist()[:2], [4.0, 0.0])
        self.assertTrue(np.isnan(o["value"].tolist()[2]))

    def test_histogram_columns(self):
        """APM 1m 的指標文件：直方圖一格 {values, counts}，或攤成兩欄。"""
        h = {"values": [100_000.0, 200_000.0, 2_500_000.0], "counts": [8, 1, 1]}
        base = {"@timestamp": T0.isoformat(), "service.name": "a", "transaction.name": "r", "metricset.name": "transaction"}
        r1 = pd.DataFrame([{**base, "transaction.duration.histogram": h, "event.outcome": "success"},
                           {**base, "transaction.duration.histogram": {"values": [300_000.0], "counts": [2]}, "event.outcome": "failure"}])
        r2 = pd.DataFrame([{**base, "transaction.duration.histogram.values": h["values"], "transaction.duration.histogram.counts": h["counts"],
                            "event.outcome": "success"},
                           {**base, "transaction.duration.histogram.values": [300_000.0], "transaction.duration.histogram.counts": [2],
                            "event.outcome": "failure"}])
        f = "transaction.duration.histogram"
        for r in (r1, r2):
            c = S.series_from_rows(r, self.spec(value={"hist_count": f}), T0, self.END, "1m")
            self.assertEqual((c["value"][0], c["n"][0]), (12, 12))
            e = S.series_from_rows(r, self.spec(value={"hist_ratio": {"field": f, "query": {"term": {"event.outcome": "failure"}}}}), T0, self.END, "1m")
            self.assertAlmostEqual(e["value"][0], 2 / 12)
            p = S.series_from_rows(r, self.spec(value={"percentile": {"field": f, "percent": 95}}, scale=1e-6), T0, self.END, "1m")
            self.assertAlmostEqual(p["value"][0], 2.5)                    # 第 12 個（95% of 12 = 11.4 → 第 12 個）是 2.5 秒
            a = S.series_from_rows(r, self.spec(value={"hist_ratio_above": {"field": f, "gt": 2.0}}, scale=1e-6), T0, self.END, "1m")
            self.assertAlmostEqual(a["value"][0], 1 / 12)

    def test_hist_on_plain_events_explains(self):
        with self.assertRaises(S.ShapeError) as cm:
            S.series_from_rows(rows(), self.spec(value={"hist_count": "transaction.duration.histogram"}), T0, self.END, "1m")
        self.assertIn("直方圖", str(cm.exception))
        self.assertIn("序列 t", str(cm.exception))

    def test_missing_by_column_explains(self):
        with self.assertRaises(S.ShapeError) as cm:
            S.series_from_rows(rows(), self.spec(value="count", by=["service.name", "nope"]), T0, self.END, "1m")
        self.assertIn("nope", str(cm.exception))


class SeriesFormatTest(unittest.TestCase):
    def test_already_shaped_rows(self):
        r = pd.DataFrame({"ts": [T0, T0 + pd.Timedelta(seconds=30), T0 + pd.Timedelta(minutes=1)], "service": ["a"] * 3, "route": ["r"] * 3,
                          "value": [0.1, 0.3, 0.2], "n": [10, 30, 20]})
        spec = {"name": "s", "index": "x", "by": ["service.name", "transaction.name"], "keys": ["service", "route"], "value": {"avg": "x"}}
        df = S.series_from_rows(r, spec, T0, T0 + pd.Timedelta(minutes=2), "1m", fmt="series")
        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(df.sort_values("ts")["value"].iloc[0], (0.1 * 10 + 0.3 * 30) / 40)   # 同一窗兩列：依 n 加權
        self.assertEqual(df.sort_values("ts")["n"].iloc[0], 40)
        c = S.series_from_rows(r, {**spec, "value": "count"}, T0, T0 + pd.Timedelta(minutes=2), "1m", fmt="series")
        self.assertEqual(c.sort_values("ts")["value"].iloc[0], 0.4)                                  # 筆數類相加


class PointsTest(unittest.TestCase):
    def test_points_grouped_sorted_with_join(self):
        r = pd.DataFrame([
            {"@timestamp": (T0 + pd.Timedelta(minutes=1)).isoformat(), "pod": "p1", "metrics.used": 5},
            {"@timestamp": T0.isoformat(), "pod": "p1", "metrics.used": 3},
            {"@timestamp": T0.isoformat(), "pod": "p1", "metrics.max": 10},
            {"@timestamp": T0.isoformat(), "pod": "p2", "metrics.used": 1},
        ])
        rule = {"name": "pool", "type": "ratio", "field": "metrics.used", "den": ["metrics.max"], "by": ["pod"], "op": ">=", "threshold": 0.5,
                "join": {"field": "metrics.max"}}
        pts = S.points_from_rows(r, rule, T0, T0 + pd.Timedelta(minutes=5))
        self.assertEqual(set(pts), {("p1",), ("p2",)})
        self.assertEqual([t for t, _ in pts[("p1",)]], [T0, T0 + pd.Timedelta(minutes=1)])      # 依時間排
        self.assertEqual(pts[("p1",)][0][1]["metrics.max"], 10)                                  # join 補進同時間點
        self.assertIsNone(pts[("p1",)][1][1].get("metrics.max"))
        from shared.connection import get_field
        self.assertEqual(get_field(pts[("p1",)][0][1], "metrics.used"), 3)                       # 攤平的 dict，get_field 讀得到

"""資料品質檢查（aiops_baseline.quality）的計算：路由沒參數化、欄位缺、資料停了、落到 unassigned 變多。"""
import unittest

import pandas as pd

from aiops_baseline import quality as Q


class LooksLikeId(unittest.TestCase):
    def test_ids(self):
        for v in ["/orders/123", "/lots/550e8400-e29b-41d4-a716-446655440000", "/files/9f86d081884c7d65",
                  "/orders/A20261023001", "/search?id=42"]:
            self.assertTrue(Q.looks_like_id(v), v)

    def test_templates_and_words(self):
        for v in ["/orders/{id}", "/lots/:lotId", "/api/v1/checkout", "/health", "GET /users/{userId}/orders", "/oauth2"]:
            self.assertFalse(Q.looks_like_id(v), v)


class Cardinality(unittest.TestCase):
    keys = ["service"]

    def now(self, n, examples=()):
        return pd.DataFrame([{"service": "order", "values": n, "examples": list(examples)}])

    def ref(self, n):
        return pd.DataFrame([{"service": "order", "values": n}])

    def test_steady_ok(self):
        self.assertEqual(Q.eval_cardinality(self.now(40), self.ref(38), self.keys), [])

    def test_surge(self):
        h = Q.eval_cardinality(self.now(400), self.ref(40), self.keys)
        self.assertEqual(len(h), 1)
        self.assertIn("從 40 個變 400 個", h[0]["why"])

    def test_surge_needs_both_ratio_and_increase(self):
        # 3 倍但只多 20 個：小服務正常加 API，不算
        self.assertEqual(Q.eval_cardinality(self.now(30), self.ref(10), self.keys), [])

    def test_new_service_counts_from_zero(self):
        self.assertEqual(len(Q.eval_cardinality(self.now(80), None, self.keys)), 1)

    def test_max_values(self):
        h = Q.eval_cardinality(self.now(600), self.ref(590), self.keys, max_values=500)
        self.assertEqual(len(h), 1)
        self.assertIn("超過上限 500", h[0]["why"][0])

    def test_id_like_examples(self):
        ex = [f"/orders/{i}" for i in range(60)] + ["/orders/{id}", "/health"]
        h = Q.eval_cardinality(self.now(62, ex), self.ref(60), self.keys)
        self.assertEqual(len(h), 1)
        self.assertTrue(any("像帶了 ID" in w for w in h[0]["why"]))
        self.assertEqual(h[0]["examples"], ["/orders/0", "/orders/1", "/orders/2"])

    def test_id_like_but_few_values_ok(self):
        # 只有幾條路由，就算像 ID（例如 /v2/...）也不報
        self.assertEqual(Q.eval_cardinality(self.now(3, ["/a/1", "/a/2", "/b"]), self.ref(3), self.keys), [])


class Missing(unittest.TestCase):
    keys = ["namespace"]
    fields = ["service.name"]

    def df(self, docs, missing):
        return pd.DataFrame([{"namespace": "mes", "docs": docs, "missing:service.name": missing}])

    def test_ok(self):
        self.assertEqual(Q.eval_missing(self.df(1000, 10), None, self.keys, self.fields), [])

    def test_over_limit(self):
        h = Q.eval_missing(self.df(1000, 300), None, self.keys, self.fields)
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["field"], "service.name")
        self.assertAlmostEqual(h[0]["value"], 0.3)

    def test_too_few_docs_skipped(self):
        self.assertEqual(Q.eval_missing(self.df(50, 50), None, self.keys, self.fields), [])

    def test_increase_vs_ref(self):
        # trace.id 本來就缺一半，只看「比昨天多缺很多」
        h = Q.eval_missing(self.df(1000, 900), self.df(1000, 500), self.keys, self.fields,
                           max_missing=None, max_increase=0.3)
        self.assertEqual(len(h), 1)
        self.assertIn("從 50% 變 90%", h[0]["why"][0])
        self.assertEqual(Q.eval_missing(self.df(1000, 600), self.df(1000, 500), self.keys, self.fields,
                                        max_missing=None, max_increase=0.3), [])

    def test_increase_without_ref_not_judged(self):
        self.assertEqual(Q.eval_missing(self.df(1000, 900), None, self.keys, self.fields,
                                        max_missing=None, max_increase=0.3), [])


class Stopped(unittest.TestCase):
    keys = ["service"]

    def test_stopped(self):
        before = pd.DataFrame([{"service": "a", "docs": 100}, {"service": "b", "docs": 100}])
        recent = pd.DataFrame([{"service": "a", "docs": 5}])
        h = Q.eval_stopped(before, recent, self.keys)
        self.assertEqual([x["key"] for x in h], [{"service": "b"}])

    def test_quiet_before_not_judged(self):
        before = pd.DataFrame([{"service": "a", "docs": 3}])
        self.assertEqual(Q.eval_stopped(before, pd.DataFrame(columns=["service", "docs"]), self.keys), [])

    def test_everything_stopped(self):
        before = pd.DataFrame([{"service": "a", "docs": 50}, {"service": "b", "docs": 50}])
        self.assertEqual(len(Q.eval_stopped(before, None, self.keys)), 2)


class Share(unittest.TestCase):
    def test_increase(self):
        h = Q.eval_share(300, 1000, 100, 1000, min_increase=0.1)
        self.assertEqual(len(h), 1)
        self.assertIn("從 10% 變 30%", h[0]["why"])

    def test_steady_ok(self):
        self.assertEqual(Q.eval_share(110, 1000, 100, 1000), [])

    def test_max_share(self):
        self.assertEqual(len(Q.eval_share(100, 1000, None, None, max_share=0.05)), 1)

    def test_too_few_docs(self):
        self.assertEqual(Q.eval_share(50, 50, 0, 1000), [])


class Present(unittest.TestCase):
    def test_legacy_metric_name_reported(self):
        now = pd.DataFrame([{"service": "old-java", "docs": 12}, {"service": "zero", "docs": 0}])
        h = Q.eval_present(now, ["service"], min_docs=1)
        self.assertEqual([x["key"] for x in h], [{"service": "old-java"}])
        self.assertEqual(h[0]["why"], ["最近有 12 筆"])

    def test_nothing_present(self):
        self.assertEqual(Q.eval_present(pd.DataFrame(columns=["service", "docs"]), ["service"]), [])


class Describe(unittest.TestCase):
    def test_message(self):
        h = {"key": {"service": "order"}, "why": ["從 40 個變 400 個"], "examples": ["/orders/1"]}
        self.assertEqual(Q.describe({"name": "quality.route_cardinality", "title": "路由數暴增"}, h),
                         "路由數暴增：order，從 40 個變 400 個（例如 /orders/1）")


class Lag(unittest.TestCase):
    keys = ["service"]

    def df(self, rows):
        return pd.DataFrame([{"service": s, "docs": n, "p50": p50, "p95": p95} for s, n, p50, p95 in rows])

    def test_ingest_lag_over_limit(self):
        h = Q.eval_lag(self.df([("a", 500, 2, 180), ("b", 500, 1, 5)]), self.keys, stat="p95", max_seconds=60)
        self.assertEqual([x["key"] for x in h], [{"service": "a"}])
        self.assertIn("p95 180 秒", h[0]["why"][0])

    def test_too_few_docs(self):
        self.assertEqual(Q.eval_lag(self.df([("a", 10, 2, 999)]), self.keys, min_docs=50), [])

    def test_clock_skew_both_directions(self):
        # 應用的時間比節點慢 8 小時（時區寫錯）、或快 10 分鐘（時鐘偏了），都要報
        h = Q.eval_lag(self.df([("tz", 500, 28800, 28801), ("fast", 500, -600, -590), ("ok", 500, 0.3, 1)]),
                       self.keys, stat="p50", max_seconds=300, absolute=True)
        self.assertEqual([x["key"]["service"] for x in h], ["tz", "fast"])
        self.assertIn("-600", h[1]["why"][0])

    def test_one_direction_ignores_negative(self):
        self.assertEqual(Q.eval_lag(self.df([("a", 500, -999, -999)]), self.keys, stat="p95", max_seconds=60), [])

    def test_missing_percentile(self):
        self.assertEqual(Q.eval_lag(self.df([("a", 500, float("nan"), float("nan"))]), self.keys), [])


if __name__ == "__main__":
    unittest.main()

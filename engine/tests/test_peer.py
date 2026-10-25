"""跟兄弟比（detector/peer.py）的單元測試，不需要 ES。"""
import unittest

import pandas as pd
import yaml

from detector import peer as P


def pods(values, svc="order-api", **extra):
    return pd.DataFrame([{"service": svc, "pod": f"{svc}-{i}", "value": v, **extra} for i, v in enumerate(values)])


def hits(res):
    return list(res[res["anomaly"]]["pod"])


class CompareTest(unittest.TestCase):
    def test_one_pod_errors_others_fine(self):
        # 10 個 Pod，只有一個錯誤率 100%：整個服務平均才 10%，但跟兄弟比一眼看出來
        res = P.compare(pods([0.0] * 9 + [1.0]), ["service"], "pod", min_abs=0.05)
        self.assertEqual(hits(res), ["order-api-9"])
        r = res[res["pod"] == "order-api-9"].iloc[0]
        self.assertEqual((r["peers"], r["median"]), (9, 0.0))

    def test_all_pods_bad_is_not_a_peer_problem(self):
        # 全部一起壞（例如下游 DB 掛了）：不是「某個 Pod」的問題，交給 baseline
        self.assertEqual(hits(P.compare(pods([0.9, 0.95, 1.0, 0.92]), ["service"], "pod", min_abs=0.05)), [])

    def test_normal_spread_not_reported(self):
        # Pod 之間本來就有點差：p95 120～180ms
        res = P.compare(pods([0.12, 0.15, 0.18, 0.14, 0.16]), ["service"], "pod", min_abs=0.1, min_ratio=2)
        self.assertEqual(hits(res), [])

    def test_slow_pod_needs_both_abs_and_ratio(self):
        base = [0.010, 0.011, 0.012, 0.010]
        # 10ms → 60ms：倍數夠，但只慢 50ms，不到 100ms
        self.assertEqual(hits(P.compare(pods(base + [0.06]), ["service"], "pod", min_abs=0.1, min_ratio=2)), [])
        # 10ms → 1.5 秒：兩個都過
        self.assertEqual(hits(P.compare(pods(base + [1.5]), ["service"], "pod", min_abs=0.1, min_ratio=2)),
                         ["order-api-4"])

    def test_needs_min_peers(self):
        # 只有 2 個 Pod：分不出誰才是怪的
        self.assertEqual(len(P.compare(pods([0.0, 1.0]), ["service"], "pod", min_abs=0.05)), 0)
        self.assertEqual(hits(P.compare(pods([0.0, 0.0, 1.0]), ["service"], "pod", min_abs=0.05)), ["order-api-2"])

    def test_groups_are_separate(self):
        # 不同服務不互比：report 本來就比 order 慢
        df = pd.concat([pods([0.01, 0.01, 0.01], svc="order"), pods([2.0, 2.1, 1.9], svc="report")])
        self.assertEqual(hits(P.compare(df, ["service"], "pod", min_abs=0.1, min_ratio=2)), [])

    def test_lower_is_fine_when_direction_up(self):
        self.assertEqual(hits(P.compare(pods([0.5, 0.5, 0.5, 0.0]), ["service"], "pod", min_abs=0.05)), [])


class DescribeTest(unittest.TestCase):
    def test_huge_z_capped(self):
        r = {"service": "order-api", "pod": "order-api-9", "value": 0.13, "peers": 2, "median": 0.0, "z": 1.3e8}
        msg = P.describe({"title": "錯誤率比兄弟高"}, r, ["service", "pod"], lambda x: f"{x:.0%}")
        self.assertEqual(msg, "錯誤率比兄弟高：order-api、order-api-9（13%，其他 2 個 Pod 中位數 0%，z=>999）")


class SpecTest(unittest.TestCase):
    def test_volume_spec_from_histogram(self):
        with open("configs/baseline/peer.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        by = {c["name"]: c for c in cfg["checks"]}
        v = P.volume_spec(by["peer.http_error_rate"])
        self.assertEqual(v["value"], {"hist_count": "metrics.http.server.request.duration"})
        self.assertIsNone(P.volume_spec(by["peer.pod_cpu"]))   # CPU 不用看請求數
        self.assertEqual(P.count_spec(by["peer.pod_cpu"])["value"], "count")


if __name__ == "__main__":
    unittest.main()

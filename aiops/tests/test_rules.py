"""規則（aiops_baseline/rules.py）的單元測試，不需要 ES。"""
import unittest

import pandas as pd
import yaml

from aiops_baseline import rules as R

NOW = pd.Timestamp("2026-10-10T10:00:00Z")


def pts(*vals, every="30s", field="metrics.x", extra=None):
    """最後一個點在 NOW，往前每 30 秒一個。"""
    n = len(vals)
    out = []
    for i, v in enumerate(vals):
        d = {"metrics": {field.split(".", 1)[1]: v}} if v is not None else {"metrics": {}}
        d["metrics"].update(extra[i] if extra else {})
        out.append((NOW - pd.Timedelta(every) * (n - 1 - i), d))
    return out


class IncreaseTest(unittest.TestCase):
    def test_counter_reset_counts_from_zero(self):
        self.assertEqual(R.increase([3, 4, 4, 0, 1]), 2)   # 3→4 一次；歸零後 0→1 又一次

    def test_restart_fires(self):
        rule = {"type": "increase", "field": "metrics.x", "op": ">=", "threshold": 1}
        hits = R.evaluate(rule, {("ns", "pod"): pts(5, 5, 6)}, NOW)
        self.assertEqual([(h["key"], h["value"]) for h in hits], [(("ns", "pod"), 1.0)])
        self.assertEqual(R.evaluate(rule, {("ns", "pod"): pts(5, 5, 5)}, NOW), [])


class HoldTest(unittest.TestCase):
    rule = {"type": "compare", "field": "metrics.available", "other": "metrics.desired", "op": ">=", "threshold": 1, "for": "3m"}

    def series(self, available):
        return {("ns", "api"): pts(*available, field="metrics.available", extra=[{"desired": 2}] * len(available))}

    def test_short_dip_during_rollout_ignored(self):
        self.assertEqual(R.evaluate(self.rule, self.series([2, 2, 2, 2, 1, 1]), NOW), [])   # 只少了 30 秒

    def test_sustained_shortage_fires(self):
        hits = R.evaluate(self.rule, self.series([0] * 8), NOW)                            # 連續 3.5 分鐘
        self.assertEqual(hits[0]["value"], 2)

    def test_crash_loop_flapping_fires_with_min_ratio(self):
        # 一直重啟：每次起來 20～30 秒（實測 crash-app 的樣子）。要求「全部」永遠不成立，80% 才抓得到
        flapping = [0, 0, 0, 0, 2, 0, 0]
        self.assertEqual(R.evaluate(self.rule, self.series(flapping), NOW), [])
        self.assertEqual(len(R.evaluate({**self.rule, "min_ratio": 0.8}, self.series(flapping), NOW)), 1)

    def test_rollout_dip_not_fired_with_min_ratio(self):
        self.assertEqual(R.evaluate({**self.rule, "min_ratio": 0.8}, self.series([2, 2, 2, 2, 1, 1, 1]), NOW), [])

    def test_deleted_pod_stops_firing(self):
        rule = {"type": "value", "field": "metrics.phase", "op": "==", "threshold": 4}
        old = [(NOW - pd.Timedelta("5min"), {"metrics": {"phase": 4}})]                      # 5 分鐘前最後一筆
        self.assertEqual(R.evaluate(rule, {("ns", "pod"): old}, NOW), [])

    def test_not_enough_history_yet(self):
        self.assertEqual(R.evaluate(self.rule, self.series([0]), NOW), [])                 # 只有一個點，還不能說「一直」


class RatioTest(unittest.TestCase):
    rule = {"type": "ratio", "field": "metrics.ws", "den": ["metrics.ws", "metrics.avail"], "op": ">=", "threshold": 0.9}

    def test_memory_near_limit(self):
        d = {("ns", "pod", "app"): pts(60, field="metrics.ws", extra=[{"avail": 4}])}
        self.assertAlmostEqual(R.evaluate(self.rule, d, NOW)[0]["value"], 60 / 64)

    def test_no_limit_no_judgement(self):
        d = {("ns", "pod", "app"): pts(60, field="metrics.ws")}                             # 沒設上限 → 沒有 available
        self.assertEqual(R.evaluate(self.rule, d, NOW), [])


class ConfigTest(unittest.TestCase):
    def test_rules_yaml_is_valid(self):
        with open("baseline/rules.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        for r in cfg["rules"]:
            self.assertIn(r["type"], ("value", "compare", "ratio", "increase"), r["name"])
            self.assertIn(r.get("op", ">="), R.OPS, r["name"])
            self.assertTrue(r["by"] and r["index"] and r["field"] and r["title"], r["name"])
            if r["type"] == "compare":
                self.assertIn("other", r, r["name"])
            if r["type"] == "ratio":
                self.assertIn("den", r, r["name"])
            self.assertLess(R._dur(r.get("for", 0)), R._dur(cfg["lookback"]), r["name"])

    def test_describe(self):
        rule = {"title": "容器重啟", "type": "increase", "threshold": 1}
        self.assertEqual(R.describe(rule, {"key": ("ns", "pod", "app"), "value": 2.0}), "容器重啟：ns、pod、app（2，門檻 >= 1）")


if __name__ == "__main__":
    unittest.main()

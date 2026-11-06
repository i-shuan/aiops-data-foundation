"""回測一組建議：整組測、範圍命中、有效才逐筆剔除；四種結論（docs/pdca/meta/2026-11-backtest-group.md）。假的 detect_series，不連 ES。"""
import unittest
from unittest import mock

import pandas as pd

import aiops.service.feedback.backtest as B
from aiops.core.repository import Repos
from aiops.scenario import load_scenario

SC = load_scenario()
from aiops.core.repository.baseline import BaselineRepository

NOW = pd.Timestamp("2026-11-03T00:00:00Z")
CFG = {"series": [{"name": "http.error_rate", "by": ["service", "route"], "min_count": 3}], "train": {"timezone": "Asia/Taipei"}, "interval": "1m"}
# 回測窗內「原始」的異常：wip-worker 週一 20 點（台北）3 筆、order-svc 平日 3 筆
MON20 = pd.Timestamp("2026-11-02T12:00:00Z")
RAW = pd.DataFrame({"ts": [MON20, MON20 + pd.Timedelta(minutes=1), MON20 + pd.Timedelta(minutes=2),
                           NOW - pd.Timedelta(days=3), NOW - pd.Timedelta(days=2), NOW - pd.Timedelta(days=1)],
                    "service": ["wip-worker"] * 3 + ["order-svc"] * 3, "route": ["r"] * 6, "value": [0.08] * 6, "n": [40] * 6,
                    "status": ["anomaly"] * 6})


def fake_detect_factory(effects):
    """effects：{(kind, 動作): 會壓掉哪些列的 index} —— 模擬每種調整在這批資料上的效果。"""
    def fake(es, cfg, spec, base, end, window, dopts, tz, overrides, adjs, quiet=True):
        if end != NOW or window <= pd.Timedelta(0):   # 只在最後一天回所有資料；窗長 0 的那段不回（簡化：一天一段不是這裡要測的）
            return []
        drop = set()
        for a in list(overrides) + list(adjs):
            key = (a.get("kind") or "override", a.get("tag"))
            drop |= set(effects.get(key, []))
        return [("1m", RAW.drop(index=list(drop)))]
    return fake


class Group(unittest.TestCase):
    def run_bt(self, proposals, effects, incidents=(), missed=()):
        with mock.patch.object(B, "detect_series", fake_detect_factory(effects)), \
             mock.patch.object(B, "load_adjustments", lambda es, cfg: []), \
             mock.patch.object(BaselineRepository, "load", lambda self, name, keys: pd.DataFrame({"service": ["x"]})), \
             mock.patch.object(B, "_confirmed_incidents", lambda es, since: list(incidents)), \
             mock.patch.object(B, "_missed", lambda es, since: list(missed)):
            return B.run_backtest(Repos(object(), SC), CFG, proposals, weeks=1, now=NOW)

    MUTE = {"id": "mute", "kind": "recurring_override", "series": "http\\.error_rate", "match": {"service": "^wip\\-worker$"},
            "schedule": {"cron": "0 20 * * 1", "duration": "1h"}, "effect": {"mute": True}, "tag": "mute"}
    MIN_ABS = {"id": "abs", "kind": "override", "series": "http\\.error_rate", "match": {"service": "^order\\-svc$"}, "min_abs": 0.2, "tag": "abs"}
    MIN_COUNT = {"id": "cnt", "kind": "override", "series": "http\\.error_rate", "match": {"service": "^order\\-svc$"}, "min_count": 5, "tag": "cnt"}

    def test_single_effective(self):
        r = self.run_bt(self.MUTE, {("recurring_override", "mute"): [0, 1, 2]})
        self.assertEqual((r["verdict"], r["recommended"], r["anomalies_before"], r["anomalies_after"], r["scope_hits"]), ("effective", True, 6, 3, 3))
        self.assertNotIn("ablation", r)

    def test_only_together_is_effective(self):
        """各自單獨都壓不掉、合起來才壓得掉：一筆一筆測會兩筆都被殺；一組測是有效、而且兩筆都必要。"""
        effects = {("override", "abs"): [], ("override", "cnt"): []}
        both = {("override", "abs"): [], ("override", "cnt"): []}
        # 模擬「要兩個都在才有效」：用一個特殊 key
        def fake(es, cfg, spec, base, end, window, dopts, tz, overrides, adjs, quiet=True):
            if end != NOW or window <= pd.Timedelta(0):
                return []
            tags = {a.get("tag") for a in overrides}
            return [("1m", RAW.drop(index=[3, 4, 5]) if {"abs", "cnt"} <= tags else RAW)]
        with mock.patch.object(B, "detect_series", fake), mock.patch.object(B, "load_adjustments", lambda es, cfg: []), \
             mock.patch.object(BaselineRepository, "load", lambda self, name, keys: pd.DataFrame({"service": ["x"]})), \
             mock.patch.object(B, "_confirmed_incidents", lambda es, since: []), mock.patch.object(B, "_missed", lambda es, since: []):
            alone = B.run_backtest(Repos(object(), SC), CFG, self.MIN_ABS, weeks=1, now=NOW)
            group = B.run_backtest(Repos(object(), SC), CFG, [self.MIN_ABS, self.MIN_COUNT], weeks=1, now=NOW)
        self.assertEqual(alone["verdict"], "ineffective")
        self.assertEqual((group["verdict"], group["anomalies_after"]), ("effective", 3))
        self.assertEqual(group["ablation"], {"abs": "necessary", "cnt": "necessary"})
        self.assertEqual(group["group"], ["abs", "cnt"])

    def test_redundant_member_is_flagged(self):
        """min_abs 自己就壓得掉、min_count 拿掉也一樣 → 整組有效，min_count 多餘。"""
        r = self.run_bt([self.MIN_ABS, self.MIN_COUNT], {("override", "abs"): [3, 4, 5], ("override", "cnt"): [3]})
        self.assertEqual(r["verdict"], "effective")
        self.assertEqual(r["ablation"], {"abs": "necessary", "cnt": "redundant"})
        self.assertIn("多餘", r["reason"])

    def test_no_scope_hit_is_unknown_not_ineffective(self):
        """建議是每月最後一天：回測窗內沒有那個時段的異常 → 無法判斷，不是無效。"""
        p = {**self.MUTE, "id": "eom", "schedule": {"cron": "0 20 * * *", "duration": "1h", "days_of_month": [-1]}, "tag": "eom"}
        r = self.run_bt(p, {})
        self.assertEqual((r["verdict"], r["scope_hits"]), ("unknown", 0))
        self.assertIn("無法判斷", r["reason"])
        r2 = self.run_bt(self.MUTE, {})                       # 有命中 3 筆但一筆沒少 → 無效
        self.assertEqual((r2["verdict"], r2["scope_hits"]), ("ineffective", 3))
        self.assertIn("無效", r2["reason"])

    def test_harmful_group_is_rejected_whole(self):
        inc = [{"id": "i1", "title": "order-svc 事故", "services": ["order-svc"], "start": (NOW - pd.Timedelta(days=2, hours=1)).isoformat(),
                "end": (NOW - pd.Timedelta(days=2) + pd.Timedelta(hours=1)).isoformat()}]
        r = self.run_bt([self.MIN_ABS, self.MIN_COUNT], {("override", "abs"): [3, 4, 5], ("override", "cnt"): []}, incidents=inc)
        self.assertEqual((r["verdict"], r["recommended"]), ("harmful", False))
        self.assertNotIn("ablation", r)

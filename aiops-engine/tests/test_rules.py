"""規則（detector/rules.py）的單元測試，不需要 ES。"""
import unittest

import pandas as pd
import yaml

import detect.rules as R

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

    def test_gone_pod_counter_not_reported(self):
        rule = {"type": "increase", "field": "metrics.x", "op": ">=", "threshold": 1}
        old = [(NOW - pd.Timedelta(minutes=m), {"metrics": {"x": v}}) for m, v in ((7, 1), (6, 30), (5, 45))]
        self.assertEqual(R.evaluate(rule, {("ns", "gone-pod"): old}, NOW), [])            # 5 分鐘前就沒資料了
        self.assertEqual(len(R.evaluate({**rule, "report_gone": True}, {("ns", "gone-pod"): old}, NOW)), 1)

    def test_gauge_delta_not_counter_reset(self):
        # Kafka 落後是量測值：一直變少（追上來）不能被當成「歸零重來」算成增加
        catching_up = pts(20000, 19000, 18000, 17000, field="metrics.lag")
        growing = pts(10000, 12000, 14000, 16000, field="metrics.lag")
        inc = {"type": "increase", "field": "metrics.lag", "op": ">=", "threshold": 5000, "for": "5m"}
        self.assertEqual(len(R.evaluate(inc, {("g", "t"): catching_up}, NOW)), 1)       # increase 的誤判（所以要 delta）
        delta = {**inc, "type": "delta"}
        self.assertEqual(R.evaluate(delta, {("g", "t"): catching_up}, NOW), [])
        self.assertEqual([h["value"] for h in R.evaluate(delta, {("g", "t"): growing}, NOW)], [6000.0])

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

    def test_new_object_must_cover_whole_for(self):
        # 新建立的對象從第一筆就成立（例如新 Pod 一直 Pending）：只有 for 的一半多一點不算，蓋滿 for 才算
        self.assertEqual(R.evaluate(self.rule, self.series([0] * 4), NOW), [])            # 1.5 分鐘（for 3m）
        self.assertEqual(R.evaluate(self.rule, self.series([0] * 6), NOW), [])            # 2.5 分鐘：差一個點
        self.assertEqual(len(R.evaluate(self.rule, self.series([0] * 7), NOW)), 1)        # 3 分鐘
        # 本來就在的對象（for 開頭之前就有資料，那時還正常）：照舊看 for 裡面的點
        self.assertEqual(len(R.evaluate(self.rule, self.series([2] * 4 + [0] * 7), NOW)), 1)
        self.assertEqual(R.evaluate(self.rule, self.series([2] * 4 + [0] * 6), NOW), [])


class RatioTest(unittest.TestCase):
    rule = {"type": "ratio", "field": "metrics.ws", "den": ["metrics.ws", "metrics.avail"], "op": ">=", "threshold": 0.9}

    def test_memory_near_limit(self):
        d = {("ns", "pod", "app"): pts(60, field="metrics.ws", extra=[{"avail": 4}])}
        self.assertAlmostEqual(R.evaluate(self.rule, d, NOW)[0]["value"], 60 / 64)

    def test_no_limit_no_judgement(self):
        d = {("ns", "pod", "app"): pts(60, field="metrics.ws")}                             # 沒設上限 → 沒有 available
        self.assertEqual(R.evaluate(self.rule, d, NOW), [])


class SlopeTest(unittest.TestCase):
    """container.memory_leak：最近 30 分鐘 working set 的 Theil–Sen 斜率，照這個速度 <= 6 小時到上限（ws + available）才報。"""
    rule = {"type": "slope", "field": "metrics.ws", "den": ["metrics.ws", "metrics.avail"], "for": "30m", "min_points": 40,
            "op": "<=", "threshold": 6}

    def mem(self, ws, limit=1000):
        return {("ns", "pod", "app"): pts(*ws, field="metrics.ws", extra=[{"avail": limit - w} if limit else {} for w in ws])}

    def test_linear_leak_fires_with_hours_to_limit(self):
        # 30 分鐘從 500 漲到 800（每小時 600），上限 1000：剩 200 → 0.33 小時
        ws = [500 + 5 * i for i in range(61)]
        h, = R.evaluate(self.rule, self.mem(ws), NOW)
        self.assertAlmostEqual(h["value"], 200 / 600, places=2)
        self.assertAlmostEqual(h["per_hour"], 600, places=0)

    def test_gc_sawtooth_without_trend_not_fired(self):
        # 每 5 分鐘 GC 一次：500 → 700 再掉回 500，低點不變，不是洩漏
        ws = [500 + 20 * (i % 10) for i in range(61)]
        self.assertEqual(R.evaluate(self.rule, self.mem(ws), NOW), [])

    def test_gc_sawtooth_with_rising_lows_fires(self):
        # 鋸齒但每次 GC 後的低點都比上次高 → 洩漏
        ws = [500 + 5 * i + 20 * (i % 10) for i in range(61)]
        self.assertEqual(len(R.evaluate(self.rule, self.mem(ws), NOW)), 1)

    def test_far_from_limit_not_fired(self):
        # 同樣每小時 600，但上限 100,000：要 165 小時才到
        ws = [500 + 5 * i for i in range(61)]
        self.assertEqual(R.evaluate(self.rule, self.mem(ws, limit=100_000), NOW), [])

    def test_new_container_not_judged(self):
        # 只有 10 分鐘的資料（啟動期 heap 在長）：for 30m 沒蓋滿，不判斷
        ws = [500 + 5 * i for i in range(21)]
        self.assertEqual(R.evaluate(self.rule, self.mem(ws), NOW), [])

    def test_no_limit_no_judgement(self):
        ws = [500 + 5 * i for i in range(61)]
        self.assertEqual(R.evaluate(self.rule, self.mem(ws, limit=None), NOW), [])

    def test_flat_or_decreasing_not_fired(self):
        self.assertEqual(R.evaluate(self.rule, self.mem([700] * 61), NOW), [])
        self.assertEqual(R.evaluate(self.rule, self.mem([800 - 5 * i for i in range(61)]), NOW), [])

    def test_single_spike_does_not_make_a_trend(self):
        # 中間一個點飆到 950 又回來：Theil–Sen 不受單點影響、低點也沒墊高
        ws = [500] * 30 + [950] + [500] * 30
        self.assertEqual(R.evaluate(self.rule, self.mem(ws), NOW), [])

    def test_without_den_value_is_rise_per_hour(self):
        rule = {"type": "slope", "field": "metrics.ws", "for": "30m", "min_points": 40, "op": ">=", "threshold": 500}
        h, = R.evaluate(rule, self.mem([500 + 5 * i for i in range(61)], limit=None), NOW)
        self.assertAlmostEqual(h["value"], 600, places=0)

    def test_describe_hours(self):
        rule = {"title": "像洩漏", "type": "slope", "threshold": 6, "op": "<=", "show": "hours"}
        self.assertEqual(R.describe(rule, {"key": ("ns", "pod", "app"), "value": 1.25}), "像洩漏：ns、pod、app（1.2 小時，門檻 <= 6.0 小時）")


class NewTypesTest(unittest.TestCase):
    def test_throttle_ratio(self):
        rule = {"type": "increase_ratio", "field": "metrics.thr", "den": ["metrics.per"], "op": ">=", "threshold": 0.25, "for": "5m"}
        # 實測 cpu-burst：每 30 秒約 180 個週期、一半被限流
        d = {("ns", "pod", "app"): pts(0, 90, 180, 270, field="metrics.thr", extra=[{"per": p} for p in (0, 180, 360, 540)])}
        self.assertAlmostEqual(R.evaluate(rule, d, NOW)[0]["value"], 0.5)

    def test_gc_overhead_rate(self):
        rule = {"type": "rate", "field": "metrics.gc.sum", "op": ">=", "threshold": 0.1, "for": "3m"}
        # 每 30 秒 GC 6 秒（每個點是那 30 秒的量）→ 20%
        series = {("ns", "pod"): [(NOW - pd.Timedelta(seconds=30 * i), {"metrics": {"gc": {"sum": 6.0}}}) for i in (5, 4, 3, 2, 1, 0)]}
        self.assertAlmostEqual(R.evaluate(rule, series, NOW)[0]["value"], 36 / 180)

    def test_major_gc_count(self):
        rule = {"type": "total", "field": "metrics.gc", "hist_count": True, "op": ">=", "threshold": 5, "for": "5m"}
        h = lambda n: {"metrics": {"gc": {"scale": 38, "positive": {"indices": [1, 2], "counts": [n - 1, 1]}, "sum": 0.07 * n}}}
        # 實測 order-app OOM 前：每 30 秒 0、1、1、2、2、4 次 major GC
        series = {("ns", "pod"): [(NOW - pd.Timedelta(seconds=30 * i), h(n) if n else {"metrics": {"gc": {"sum": 0}}})
                                  for i, n in zip((5, 4, 3, 2, 1, 0), (0, 1, 1, 2, 2, 4))]}
        self.assertEqual(R.evaluate(rule, series, NOW)[0]["value"], 10)

    def test_hist_count_es8_histogram(self):
        # ES 8.19 的 histogram 在 _source 裡是 values／counts（實測 Collector elasticsearch 寫入元件寫出來的）
        self.assertEqual(R.hist_count({"values": [0.0375, 0.0625, 0.375], "counts": [48, 60, 12]}), 120)

    def test_join_fills_denominator(self):
        d = {"metrics": {"usage": 5.0}}
        from detect.store.series_repo import _put
        _put(d, "metrics.db_client_connections_max", 5.0)
        self.assertEqual(d["metrics"]["db_client_connections_max"], 5.0)
        rule = {"type": "ratio", "field": "metrics.usage", "den": ["metrics.db_client_connections_max"], "op": ">=", "threshold": 0.9}
        self.assertEqual(R.sample_value(rule, d), 1.0)


class ConfigTest(unittest.TestCase):
    def test_rules_yaml_is_valid(self):
        with open("configs/baseline/rules.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        for r in cfg["rules"]:
            self.assertIn(r["type"], ("value", "compare", "ratio", "increase", "delta", "increase_ratio", "rate", "total", "window", "slope"), r["name"])
            self.assertIn(r.get("op", ">="), R.OPS, r["name"])
            # window 用 series.yaml 的 value 寫法（hist_ratio、percentile），其他用 field
            self.assertTrue(r["by"] and r["index"] and (r.get("value") if r["type"] == "window" else r["field"]) and r["title"], r["name"])
            if r["type"] == "compare":
                self.assertIn("other", r, r["name"])
            if r["type"] in ("ratio", "increase_ratio"):
                self.assertIn("den", r, r["name"])
            self.assertLess(R._dur(r.get("for", 0)), R._dur(r.get("lookback", cfg["lookback"])), r["name"])

    def test_describe_number_ratio(self):
        rule = {"title": "執行緒暴增", "type": "ratio", "threshold": 16, "show": "number"}
        self.assertEqual(R.describe(rule, {"key": ("ns", "pod"), "value": 44.0}), "執行緒暴增：ns、pod（44，門檻 >= 16）")

    def test_describe(self):
        rule = {"title": "容器重啟", "type": "increase", "threshold": 1}
        self.assertEqual(R.describe(rule, {"key": ("ns", "pod", "app"), "value": 2.0}), "容器重啟：ns、pod、app（2，門檻 >= 1）")


class ClockOffsetTest(unittest.TestCase):
    """主機時鐘偏移：快、慢都算（abs），奈秒顯示成毫秒。"""
    rule = {"name": "node.clock_offset", "title": "主機時鐘偏移超過 100 毫秒", "type": "value", "abs": True,
            "op": ">=", "threshold": 100_000_000, "show": "ns_as_ms", "field": "metrics.ntp.offset", "by": ["n"],
            "fresh": "40m"}

    def test_abs_both_directions(self):
        import detect.rules as R
        self.assertEqual(R.sample_value(self.rule, {"metrics": {"ntp.offset": -300_000_000}}), 300_000_000)
        self.assertEqual(R.sample_value({**self.rule, "abs": False}, {"metrics": {"ntp.offset": -300_000_000}}), -300_000_000)

    def test_message_in_ms(self):
        import detect.rules as R
        msg = R.describe(self.rule, {"key": ("node-1",), "value": 2_500_460_925})
        self.assertIn("2,500 毫秒", msg)
        self.assertIn("100 毫秒", msg)


class WindowFloorTest(unittest.TestCase):
    """type window：整段算一個值（錯誤率、p95），請求數不夠的不判斷。"""
    def rule(self, **kw):
        with open("configs/baseline/rules.yaml", encoding="utf-8") as f:
            rules = {r["name"]: r for r in yaml.safe_load(f)["rules"]}
        return {**rules["http.error_rate_floor"], **kw}

    def df(self, rows):
        by = self.rule()["by"]
        return pd.DataFrame([dict(zip(by, k)) | {"value": v, "n": n} for k, v, n in rows])

    def test_error_rate_over_floor(self):
        r = self.rule()
        since = pd.Timestamp("2026-10-25T03:00:00Z")
        hits = R.evaluate_window(r, self.df([(("order", "GET", "/a"), 0.12, 200), (("order", "GET", "/b"), 0.01, 500)]), since)
        self.assertEqual([h["key"] for h in hits], [("order", "GET", "/a")])
        self.assertEqual(R.describe(r, hits[0]), "API 錯誤率超過 5%：order、GET、/a（12%，門檻 >= 5%）")

    def test_too_few_requests_not_judged(self):
        # 5 分鐘 3 個請求錯 1 個：不是「錯誤率 33%」
        self.assertEqual(R.evaluate_window(self.rule(), self.df([(("order", "GET", "/a"), 0.33, 3)]), pd.Timestamp.now(tz="UTC")), [])

    def test_no_value(self):
        self.assertEqual(R.evaluate_window(self.rule(), self.df([(("order", "GET", "/a"), float("nan"), 0)]), pd.Timestamp.now(tz="UTC")), [])

    def test_latency_shown_in_seconds(self):
        with open("configs/baseline/rules.yaml", encoding="utf-8") as f:
            r = {x["name"]: x for x in yaml.safe_load(f)["rules"]}["http.latency_p95_floor"]
        hit = R.evaluate_window(r, self.df([(("order", "GET", "/a"), 2.5, 100)]), pd.Timestamp.now(tz="UTC"))[0]
        self.assertEqual(R.describe(r, hit), "API p95 超過 2 秒：order、GET、/a（2.50 秒，門檻 >= 2.00 秒）")


if __name__ == "__main__":
    unittest.main()

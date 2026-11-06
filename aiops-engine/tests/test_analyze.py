"""analyze 的單元測試，不需要 ES。"""
import unittest

import incident.analyze as A
import detect.store.trace as T


def span(sid, parent, service, name, start, dur, kind="Server", target=None, error=False, trace="t1", resource=None, attrs=None):
    return {"trace_id": trace, "span_id": sid, "parent": parent, "service": service, "name": name, "kind": kind,
            "target": target, "start_ms": float(start), "dur_ms": float(dur), "error": error,
            "resource": resource or {}, "attrs": attrs or {}}


def request(trace, db_ms, error=False):
    """mes-api → wip-worker → Oracle。db_ms：查 Oracle 花多久。"""
    w = 10 + db_ms
    return [
        span("a", None, "mes-api", "GET /lots/{id}", 0, w + 20, trace=trace, error=error),
        span("b", "a", "mes-api", "GET", 5, w + 5, kind="Client", target="wip-worker", trace=trace, error=error),
        span("c", "b", "wip-worker", "GET /wip", 7, w, trace=trace, error=error),
        span("d", "c", "wip-worker", "SELECT LOT_HISTORY", 12, db_ms, kind="Client", target="oracle", trace=trace, error=error),
    ]


class SelfTimeTest(unittest.TestCase):
    def test_waiting_for_child_is_not_self_time(self):
        st = T.self_times(request("t1", 3000))
        self.assertEqual(st["d"], 3000)       # 等 Oracle 的那段
        self.assertEqual(st["c"], 10)         # wip-worker 自己只有 10ms
        self.assertEqual(st["a"], 15)

    def test_overlapping_children_counted_once(self):
        t = [span("p", None, "s", "x", 0, 100), span("c1", "p", "s", "y", 10, 50, kind="Client"),
             span("c2", "p", "s", "z", 30, 50, kind="Client")]   # 平行呼叫：10～80 被佔掉
        self.assertEqual(T.self_times(t)["p"], 30)


class CompareTest(unittest.TestCase):
    def test_bottleneck_ranked_first(self):
        now = T.group_traces([s for i in range(5) for s in request(f"n{i}", 3000)])
        ref = T.group_traces([s for i in range(5) for s in request(f"r{i}", 30)])
        top = A.compare_segments(now, ref)[0]
        self.assertEqual(top.segment, "wip-worker › SELECT LOT_HISTORY → oracle")
        self.assertAlmostEqual(top.delta_ms, 2970)

    def test_error_origin_is_deepest_error(self):
        origins = T.error_origins(request("t1", 30, error=True))
        self.assertEqual([s["span_id"] for s in origins], ["d"])   # 上面三層只是把錯誤往上傳


class TopologyTest(unittest.TestCase):
    def test_edges(self):
        tr = T.group_traces(request("t1", 30) + request("t2", 40))
        edges = {(e.caller, e.callee): e.calls for e in T.topology(tr)}
        self.assertEqual(edges, {("mes-api", "wip-worker"): 2, ("wip-worker", "oracle"): 2})

    def test_waterfall_has_every_span(self):
        lines = A.waterfall(request("t1", 3000))
        self.assertEqual(len(lines), 4)
        self.assertIn("SELECT LOT_HISTORY", lines[3])
        self.assertTrue(lines[3].startswith("      "))              # 第 4 層，縮排 3 次


class CommonalityTest(unittest.TestCase):
    """慢請求的共同點：出事時那批最慢的 vs 平常那批。"""

    def req(self, trace, pod, lot="L1", sql="SELECT 1"):
        res = {"k8s.pod.name": pod, "service.version": "1.0.0"}
        return [span("a", None, "mes-api", "GET /lots/{id}", 0, 50, trace=trace, resource={"k8s.pod.name": "mes-1"}, attrs={"lot.id": lot}),
                span("d", "a", "wip-worker", "SELECT", 5, 40, kind="Client", target="oracle", trace=trace, resource=res, attrs={"db.statement": sql})]

    def test_slow_requests_all_on_one_pod(self):
        # 慢的 10 個都落在 wip-worker 的 w-bad；平常 3 個 Pod 輪流（各 1/3）
        now = T.group_traces([s for i in range(10) for s in self.req(f"n{i}", "w-bad")])
        ref = T.group_traces([s for i in range(12) for s in self.req(f"r{i}", ["w-1", "w-2", "w-bad"][i % 3])])
        c = A.commonality(now, ref)
        self.assertEqual((c[0].dim, c[0].value, c[0].now, c[0].now_share), ("k8s.pod.name", "wip-worker/w-bad", 10, 1.0))
        self.assertAlmostEqual(c[0].ref_share, 1 / 3)
        self.assertFalse(c[0].is_new)
        # 版本、mes-api 的 Pod、lot、SQL 兩邊都一樣集中：不列
        self.assertEqual([x.dim for x in c], ["k8s.pod.name"])

    def test_pod_unseen_before_is_flagged_new(self):
        # 換版：慢的請求全在新 Pod，平常那批是舊 Pod（0%）→ 列出來但標「平常沒見過」，不是「這個 Pod 比別的慢」
        now = T.group_traces([s for i in range(10) for s in self.req(f"n{i}", "w-new")])
        ref = T.group_traces([s for i in range(10) for s in self.req(f"r{i}", "w-old")])
        c = A.commonality(now, ref)
        self.assertEqual((c[0].value, c[0].ref_share, c[0].is_new), ("wip-worker/w-new", 0.0, True))

    def test_single_pod_is_not_a_finding(self):
        now = T.group_traces([s for i in range(10) for s in self.req(f"n{i}", "w-1")])
        ref = T.group_traces([s for i in range(10) for s in self.req(f"r{i}", "w-1")])
        self.assertEqual(A.commonality(now, ref), [])

    def test_sql_in_common(self):
        now = T.group_traces([s for i in range(10) for s in self.req(f"n{i}", ["w-1", "w-2"][i % 2], sql="SELECT * FROM LOT_HISTORY")])
        ref = T.group_traces([s for i in range(10) for s in self.req(f"r{i}", ["w-1", "w-2"][i % 2], sql=["SELECT 1", "SELECT * FROM LOT_HISTORY"][i % 5 == 0])])
        c = A.commonality(now, ref)
        self.assertEqual((c[0].dim, c[0].value), ("db.statement", "SELECT * FROM LOT_HISTORY"))
        self.assertEqual((c[0].now_share, c[0].ref_share), (1.0, 0.2))

    def test_too_few_or_no_reference(self):
        now = T.group_traces([s for i in range(3) for s in self.req(f"n{i}", "w-bad")])
        self.assertEqual(A.commonality(now, {}), [])
        self.assertEqual(A.commonality(now, T.group_traces(self.req("r", "w-1"))), [])


class TrendTest(unittest.TestCase):
    def test_gradual(self):
        t = A.classify_trend([40, 45, 60, 90, 150, 300], 1200)
        self.assertEqual(t["kind"], "gradual")
        self.assertIn("慢慢惡化", A.describe_trend(t, "1 小時", lambda v: f"{v:.0f} ms"))

    def test_sudden(self):
        t = A.classify_trend([40, 42, 38, 41, 45, 40], 1200)
        self.assertEqual(t["kind"], "sudden")
        self.assertIn("突然", A.describe_trend(t, "1 小時", lambda v: f"{v:.0f} ms"))

    def test_mixed_and_unknown(self):
        self.assertEqual(A.classify_trend([40, 200, 40, 300, 50, 60], 1200)["kind"], "mixed")
        self.assertEqual(A.classify_trend([0, 0, 40, 0, 0, 0], 1200)["kind"], "unknown")   # 0 = 那段沒 trace
        self.assertEqual(A.classify_trend([40, 42, 38, 41, 45, 40], 50)["kind"], "mixed")   # 出事時也沒比平常高：不是突然


class PatternTest(unittest.TestCase):
    def test_same_error_different_values_is_one_pattern(self):
        a = A.log_pattern("Connection pool timeout to 10.0.1.5:5432 after 3000ms, lot L123456")
        b = A.log_pattern("Connection pool timeout to 10.0.1.6:5432 after 2950ms, lot L998877")
        self.assertEqual(a, b)

    def test_new_pattern_first(self):
        now = [{"service": "w", "message": "slow query 1"}] * 5 + [{"service": "w", "message": "pool timeout 3000ms"}] * 2
        ref = [{"service": "w", "message": "slow query 2"}] * 1
        p = A.compare_patterns(now, ref)
        self.assertTrue(p[0].is_new)
        self.assertEqual(p[0].now, 2)
        self.assertEqual((p[1].now, p[1].ref), (5, 1))


if __name__ == "__main__":
    unittest.main()

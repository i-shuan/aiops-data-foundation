"""analyze 的單元測試，不需要 ES。"""
import unittest

from aiops_rca import analyze as A


def span(sid, parent, service, name, start, dur, kind="Server", target=None, error=False, trace="t1"):
    return {"trace_id": trace, "span_id": sid, "parent": parent, "service": service, "name": name, "kind": kind,
            "target": target, "start_ms": float(start), "dur_ms": float(dur), "error": error}


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
        st = A.self_times(request("t1", 3000))
        self.assertEqual(st["d"], 3000)       # 等 Oracle 的那段
        self.assertEqual(st["c"], 10)         # wip-worker 自己只有 10ms
        self.assertEqual(st["a"], 15)

    def test_overlapping_children_counted_once(self):
        t = [span("p", None, "s", "x", 0, 100), span("c1", "p", "s", "y", 10, 50, kind="Client"),
             span("c2", "p", "s", "z", 30, 50, kind="Client")]   # 平行呼叫：10～80 被佔掉
        self.assertEqual(A.self_times(t)["p"], 30)


class CompareTest(unittest.TestCase):
    def test_bottleneck_ranked_first(self):
        now = A.group_traces([s for i in range(5) for s in request(f"n{i}", 3000)])
        ref = A.group_traces([s for i in range(5) for s in request(f"r{i}", 30)])
        top = A.compare_segments(now, ref)[0]
        self.assertEqual(top.segment, "wip-worker › SELECT LOT_HISTORY → oracle")
        self.assertAlmostEqual(top.delta_ms, 2970)

    def test_error_origin_is_deepest_error(self):
        origins = A.error_origins(request("t1", 30, error=True))
        self.assertEqual([s["span_id"] for s in origins], ["d"])   # 上面三層只是把錯誤往上傳


class TopologyTest(unittest.TestCase):
    def test_edges(self):
        tr = A.group_traces(request("t1", 30) + request("t2", 40))
        edges = {(e.caller, e.callee): e.calls for e in A.topology(tr)}
        self.assertEqual(edges, {("mes-api", "wip-worker"): 2, ("wip-worker", "oracle"): 2})

    def test_waterfall_has_every_span(self):
        lines = A.waterfall(request("t1", 3000))
        self.assertEqual(len(lines), 4)
        self.assertIn("SELECT LOT_HISTORY", lines[3])
        self.assertTrue(lines[3].startswith("      "))              # 第 4 層，縮排 3 次


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

"""trace 每一段的 baseline（detector/trace.py）的單元測試，不需要 ES。
最後一組在記憶體裡跑完整流程：模擬 4 週的請求 → 統計 → 學 → 對 docs/rca/README.md 的案例判斷。"""
import random
import unittest

import pandas as pd

from detector import trace as T
from detector.model import DetectOptions, TrainOptions, score, train

API = ("mes-api", "GET /lots/{id}")


def span(trace, sid, parent, service, name, start, dur, kind="Server", target=None, error=False):
    return {"trace_id": trace, "span_id": sid, "parent": parent, "service": service, "name": name, "kind": kind,
            "target": target, "start_ms": float(start), "dur_ms": float(dur), "error": error}


def request(tid, mode="normal", rng=random):
    """mes-api 收到 GET /lots/{id}：
    - 呼叫 wip-worker 一次（wip-worker 再查 Oracle）
    - 5% 的請求快取沒命中，自己查 Oracle「SELECT LOT」
    - 呼叫簡訊廠商一次
    mode：n_plus_1（wip-worker 查 100 次）、retry（呼叫 wip-worker 4 次）、cold_cache（每個都查 DB）、sms_hang（簡訊 3 秒）"""
    out, t = [], 0.0
    root = span(tid, "r", None, "mes-api", "GET /lots/{id}", 0, 0)
    out.append(root)
    t += 2
    if mode == "cold_cache" or rng.random() < 0.05:
        out.append(span(tid, "db", "r", "mes-api", "SELECT LOT", t, 5, "Client", "oracle"))
        t += 5
    for i in range(4 if mode == "retry" else 1):
        q = 100 if mode == "n_plus_1" else 1
        w = 2 + q * 2
        out.append(span(tid, f"c{i}", "r", "mes-api", "GET", t, w + 2, "Client", "wip-worker"))
        out.append(span(tid, f"w{i}", f"c{i}", "wip-worker", "GET /wip", t + 1, w))
        for j in range(q):
            out.append(span(tid, f"q{i}_{j}", f"w{i}", "wip-worker", "SELECT LOT_HISTORY", t + 2 + j * 2, 2, "Client", "oracle"))
        t += w + 2
    sms = 3000 if mode == "sms_hang" else rng.uniform(15, 25)
    out.append(span(tid, "sms", "r", "mes-api", "POST", t, sms, "Client", "sms-gateway"))
    t += sms
    root["dur_ms"] = t + 1
    return out


def bucket_samples(ts, n, mode="normal", rng=random):
    traces, samples = {}, []
    for i in range(n):
        tid = f"{ts.value}-{i}"
        traces[tid] = request(tid, mode, rng)
        samples.append(T.Sampled(ts, tid, "r"))
        samples.append(T.Sampled(ts, tid, "w0"))
    return samples, traces


class OwnPartTest(unittest.TestCase):
    def test_counts_and_times(self):
        t = request("t", "n_plus_1", random.Random(1))
        segs = T.request_segments(t, t[0])
        self.assertEqual(segs["mes-api › GET → wip-worker"]["calls"], 1)
        self.assertNotIn("wip-worker › SELECT LOT_HISTORY → oracle", segs)   # 下游服務裡面的事不算在 mes-api
        w = next(s for s in t if s["span_id"] == "w0")
        self.assertEqual(T.request_segments(t, w)["wip-worker › SELECT LOT_HISTORY → oracle"]["calls"], 100)

    def test_client_counts_whole_wait(self):
        t = request("t", "sms_hang", random.Random(1))
        self.assertEqual(T.request_segments(t, t[0])["mes-api › POST → sms-gateway"]["time_ms"], 3000)

    def test_missing_segment_is_zero(self):
        rng = random.Random(3)
        samples, traces = bucket_samples(pd.Timestamp("2026-10-01", tz="UTC"), 20, rng=rng)
        df, _ = T.summarize(samples, traces)
        df = T.fill_missing_segments(df)
        db = df[(df["segment"] == "mes-api › SELECT LOT → oracle")]
        self.assertTrue(((db["calls"] >= 0) & (db["calls"] < 0.5)).all())


class ClockSkewTest(unittest.TestCase):
    def test_child_before_parent(self):
        t = [span("t", "c", None, "a", "GET", 1000, 50, "Client", "b"), span("t", "s", "c", "b", "GET /x", -2500, 40)]
        self.assertEqual(T.clock_skews(t, 500), [("a", "b", 3500.0)])

    def test_normal_network_delay_ok(self):
        t = [span("t", "c", None, "a", "GET", 1000, 50, "Client", "b"), span("t", "s", "c", "b", "GET /x", 1003, 40)]
        self.assertEqual(T.clock_skews(t, 500), [])


class NewSegmentTest(unittest.TestCase):
    def test_new_call_on_known_api(self):
        base = pd.DataFrame([{"entry.service": "a", "entry.name": "GET /x", "segment": "a › GET /x"}])
        cur = pd.DataFrame([
            {"entry.service": "a", "entry.name": "GET /x", "segment": "a › GET /x", "calls": 1.0},
            {"entry.service": "a", "entry.name": "GET /x", "segment": "a › SELECT X → mysql", "calls": 1.0},   # 新的
            {"entry.service": "b", "entry.name": "GET /y", "segment": "b › SELECT Y → mysql", "calls": 1.0},   # 新 API，不算
        ])
        self.assertEqual(list(T.new_segments(cur, base)["segment"]), ["a › SELECT X → mysql"])


class EndToEndTest(unittest.TestCase):
    """4 週、每小時一個點（每個點抽 20 個請求）學出平常；再看每個案例在哪一段、哪個數字被抓到，正常的不誤報。"""

    @classmethod
    def setUpClass(cls):
        rng = random.Random(42)
        start = pd.Timestamp("2026-09-01", tz="UTC")
        samples, traces = [], {}
        for h in range(24 * 28):
            s, t = bucket_samples(start + pd.Timedelta(hours=h), 20, rng=rng)
            samples += s
            traces.update(t)
        hist, _ = T.summarize(samples, traces)
        hist = T.fill_missing_segments(hist)
        cls.base = {m: train(hist[[*T.KEYS, "ts"]].assign(value=hist[m]), T.KEYS, TrainOptions(min_points=50))
                    for m in T.METRICS}
        cls.rng = rng

    def detect(self, mode):
        ts = pd.Timestamp("2026-09-29T10:00", tz="UTC")
        samples, traces = bucket_samples(ts, 20, mode, self.rng)
        cur, _ = T.summarize(samples, traces)
        hits = set()
        rules = {"calls": ("both", 0.5), "time_ms": ("up", 20), "error_rate": ("up", 0.05)}
        for m, (direction, min_abs) in rules.items():
            c = T.fill_missing_segments(cur, self.base[m][T.KEYS].drop_duplicates())
            s = score(c[[*T.KEYS, "ts"]].assign(value=c[m]), self.base[m], T.KEYS, direction=direction,
                      min_abs=min_abs, opts=DetectOptions(z=5, rel_floor=0.1))
            hits |= {(m, r["entry.service"], r["segment"]) for r in s[s["anomaly"]].to_dict("records")}
        return hits

    def test_normal_has_no_anomaly(self):
        for _ in range(5):
            self.assertEqual(self.detect("normal"), set())

    def test_n_plus_1(self):
        self.assertIn(("calls", "wip-worker", "wip-worker › SELECT LOT_HISTORY → oracle"), self.detect("n_plus_1"))

    def test_retry_storm(self):
        self.assertIn(("calls", "mes-api", "mes-api › GET → wip-worker"), self.detect("retry"))

    def test_cold_cache(self):
        self.assertIn(("calls", "mes-api", "mes-api › SELECT LOT → oracle"), self.detect("cold_cache"))

    def test_slow_dependency(self):
        self.assertIn(("time_ms", "mes-api", "mes-api › POST → sms-gateway"), self.detect("sms_hang"))


if __name__ == "__main__":
    unittest.main()


class HardRuleTest(unittest.TestCase):
    """不用學的規則：baseline 學不出來的「從第一天就有的壞味道」。"""

    def summary(self, mode, n=20, swallow=False):
        ts = pd.Timestamp("2026-10-10T10:00:00Z")
        samples, traces = bucket_samples(ts, n, mode, random.Random(3))
        if swallow:   # wip-worker 查 DB 那段丟了例外，但程式 catch 掉、沒標成錯誤
            for t in traces.values():
                for s in t:
                    if s["name"] == "SELECT LOT_HISTORY":
                        s["exception"] = "System.TimeoutException"
        df, _ = T.summarize(samples, traces)
        return df

    def test_n_plus_one_caught_without_baseline(self):
        hits = T.repeated_calls(self.summary("n_plus_1"), 20)
        self.assertEqual(list(hits["segment"]), ["wip-worker › SELECT LOT_HISTORY → oracle"])
        self.assertEqual(hits.iloc[0]["calls"], 100)

    def test_normal_not_repeated(self):
        self.assertTrue(T.repeated_calls(self.summary("normal"), 20).empty)

    def test_swallowed_exception(self):
        df = self.summary("normal", swallow=True)
        hits = T.swallowed(df, 0.05)
        self.assertEqual(list(hits["segment"]), ["wip-worker › SELECT LOT_HISTORY → oracle"])
        self.assertEqual((hits.iloc[0]["swallowed_rate"], hits.iloc[0]["exception_type"]), (1.0, "System.TimeoutException"))
        self.assertTrue(T.swallowed(self.summary("normal"), 0.05).empty)

    def test_error_is_not_swallowed(self):
        # 有例外、標成錯誤，而且錯誤傳到入口（wip-worker 回 500）：那是錯誤（error_rate），不是吞掉
        ts = pd.Timestamp("2026-10-10T10:00:00Z")
        samples, traces = bucket_samples(ts, 5, "normal", random.Random(3))
        for t in traces.values():
            for s in t:
                if s["name"] == "SELECT LOT_HISTORY":
                    s["exception"], s["error"] = "System.TimeoutException", True
                if s["span_id"] in ("w0", "c0", "r"):
                    s["error"] = True
        df, _ = T.summarize(samples, traces)
        self.assertTrue(T.swallowed(df, 0.05).empty)
        self.assertEqual(df[df["segment"] == "wip-worker › SELECT LOT_HISTORY → oracle"]["error_rate"].iloc[0], 1.0)

    def test_error_caught_by_entry_is_swallowed(self):
        # Java agent 的樣子：JDBC 丟例外時那一段標成錯誤；程式 catch 掉、wip-worker 回 200 → 吞掉
        ts = pd.Timestamp("2026-10-10T10:00:00Z")
        samples, traces = bucket_samples(ts, 5, "normal", random.Random(3))
        for t in traces.values():
            for s in t:
                if s["name"] == "SELECT LOT_HISTORY":
                    s["exception"], s["error"] = "java.sql.SQLTimeoutException", True
        df, _ = T.summarize(samples, traces)
        hits = T.swallowed(df, 0.05)
        self.assertEqual(list(hits["segment"]), ["wip-worker › SELECT LOT_HISTORY → oracle"])
        self.assertEqual((hits.iloc[0]["swallowed_rate"], hits.iloc[0]["error_rate"]), (1.0, 1.0))

    def test_target_from_new_db_semconv(self):
        # 新版 DB 語意只有 db.system.name（沒有 server.address）：也要有「→ 對象」，N+1 規則才看得到
        from sources.traces import to_span
        s = to_span({"@timestamp": "2026-10-10T10:00:00Z", "trace_id": "t", "span_id": "s", "name": "SELECT X",
                     "kind": "Client", "duration": 2_000_000, "attributes": {"db.system.name": "oracle"},
                     "resource": {"attributes": {"service.name": "a"}}})
        self.assertEqual(s["target"], "oracle")


class ConsumerTest(unittest.TestCase):
    """訊息消費者（Kafka）：排隊時間、.NET 自動埋點的 receive（link 指回生產者）。"""

    def producer(self, tid="p"):
        return [span(tid, "h", None, "order-producer", "GET /orders/{id}", 0, 8),
                span(tid, "pub", "h", "order-producer", "orders publish", 1, 6, "Producer")]

    def test_process_child_of_producer(self):
        # 程式裡接上了 trace：process 的上一層就是 publish
        t = self.producer() + [span("p", "proc", "pub", "order-consumer", "orders process", 507, 3, "Consumer"),
                               span("p", "db", "proc", "order-consumer", "postgres", 508, 1, "Client", "postgresql")]
        entry = t[2]
        segs = T.request_segments(t, entry, T.producer_of(entry, t))
        self.assertEqual(segs["order-consumer › 排隊（orders）← order-producer"]["time_ms"], 500)   # 507 − (1 + 6)
        self.assertIn("order-consumer › postgres → postgresql", segs)

    def test_receive_linked_to_producer(self):
        # .NET 自動埋點：receive 是新的 trace，只用 link 指回生產者；它的時間是「等下一筆訊息」，不算成處理
        prod = self.producer()
        recv = dict(span("c", "recv", None, "order-consumer", "orders receive", 0, 900, "Consumer"), links=["pub"])
        entry = recv
        p = T.producer_of(entry, [recv], {"pub": prod[1]})
        segs = T.request_segments([recv], entry, p)
        self.assertEqual(segs, {"order-consumer › 排隊（orders）← order-producer":
                                {"calls": 1, "time_ms": 893, "error": False, "swallowed": None}})   # 900 − 7

    def test_linked_producer_missing(self):
        # 生產者那個 trace 沒留下（尾端取樣各自決定）：算不出排隊時間，也不報錯
        recv = dict(span("c", "recv", None, "order-consumer", "orders receive", 0, 900, "Consumer"), links=["gone"])
        self.assertIsNone(T.producer_of(recv, [recv], {}))
        self.assertEqual(T.request_segments([recv], recv, None), {})


class SampleEntriesMergedTest(unittest.TestCase):
    """index 是 traces-merged-* 時：nested → composite → top_hits；hit 的 _id 是 trace_id、_source 是入口的 span_id；只留 [start, end) 的時段。"""
    class FakeES:
        def __init__(self):
            self.bodies = []

        def search(self, index, body):
            self.bodies.append((index, body))
            if body["aggs"]["n"]["aggs"]["e"]["composite"].get("after"):
                return {"aggregations": {"n": {"e": {"buckets": []}}}}
            t0 = int(pd.Timestamp("2026-11-01T10:00:00Z").value // 10**6)
            mk = lambda svc, name, ms, ids: {"key": {"svc": svc, "name": name, "ts": float(ms)},
                                             "t": {"hits": {"hits": [{"_id": f"trace-{i}", "_source": {"span_id": f"span-{i}"}} for i in ids]}}}
            return {"aggregations": {"n": {"e": {"buckets": [
                mk("mes-api", "GET /lots/{id}", t0, ["a", "b"]),
                mk("wip-worker", "GET /wip", t0 + 300_000, ["c"]),
                mk("mes-api", "GET /lots/{id}", t0 - 300_000, ["old"]),      # 放寬查詢帶進來的、窗口外的時段：不要
                mk("mes-api", "GET /lots/{id}", t0 + 600_000, ["late"]),     # 窗口結束那一格：不要（[start, end)）
            ], "after_key": {"svc": "x"}}}}}

    def test_merged_sampling(self):
        es = self.FakeES()
        start, end = pd.Timestamp("2026-11-01T10:00:00Z"), pd.Timestamp("2026-11-01T10:10:00Z")
        got = T.sample_entries(es, "traces-merged-*", start, end, "5m", 20)
        self.assertEqual([(s.bucket.isoformat(), s.trace_id, s.span_id) for s in got],
                         [("2026-11-01T10:00:00+00:00", "trace-a", "span-a"), ("2026-11-01T10:00:00+00:00", "trace-b", "span-b"),
                          ("2026-11-01T10:05:00+00:00", "trace-c", "span-c")])
        index, body = es.bodies[0]
        self.assertEqual(index, "traces-merged-*")
        e = body["aggs"]["n"]["aggs"]["e"]
        self.assertEqual(body["aggs"]["n"]["nested"], {"path": "entries"})
        self.assertEqual(e["composite"]["sources"][2], {"ts": {"histogram": {"field": "entries.start_ms", "interval": 300000}}})
        self.assertEqual(e["aggs"]["t"]["top_hits"]["size"], 20)
        rng = body["query"]["bool"]["filter"][0]["range"]["@timestamp"]
        self.assertEqual((rng["gte"], rng["lt"]), ("2026-11-01T09:55:00+00:00", "2026-11-01T10:15:00+00:00"))   # 兩邊各放寬 5 分鐘
        self.assertEqual(len(es.bodies), 2)     # after_key 有值就翻下一頁，空的就停

    def test_raw_path_unchanged(self):
        class RawES:
            def search(self, index, body):
                self.body = body
                return {"aggregations": {"e": {"buckets": []}}}
        es = RawES()
        T.sample_entries(es, "traces-generic.otel-*", pd.Timestamp("2026-11-01T10:00:00Z"), pd.Timestamp("2026-11-01T10:10:00Z"), "5m", 20)
        self.assertIn("composite", es.body["aggs"]["e"])
        self.assertNotIn("n", es.body["aggs"])

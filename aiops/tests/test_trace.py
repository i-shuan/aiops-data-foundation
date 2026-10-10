"""trace 每一段的 baseline（aiops_baseline/trace.py）的單元測試，不需要 ES。
最後一組在記憶體裡跑完整流程：模擬 4 週的請求 → 統計 → 學 → 對 docs/aiops-cases.md 的案例判斷。"""
import random
import unittest

import pandas as pd

from aiops_baseline import trace as T
from aiops_baseline.model import DetectOptions, TrainOptions, score, train

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

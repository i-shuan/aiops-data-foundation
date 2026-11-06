"""回測（feedback.cmd_backtest）的結果怎麼呈現；回測本身要 ES 和 baseline，在 e2e 驗（docs/pdca/phase5-learning-loop/2026-10-loop-update.md）。"""
import unittest

import learn.rule_backtester as B
from shared.infra.repos import Repos
from detect.store.baseline_repo import BaselineRepository


class Format(unittest.TestCase):
    def test_recommended(self):
        r = {"weeks": 4, "series": ["http.latency_p95_s"], "anomalies_before": 30, "anomalies_after": 18, "incidents_total": 7,
             "incidents_caught_before": 7, "incidents_caught_after": 7, "incidents_lost": [], "missed_total": 0, "missed_caught_after": 0,
             "recommended": True, "reason": "告警 30 → 18；已確認事故 7/7 件還抓得到"}
        self.assertEqual(B.format_backtest(r), "回測 4 週（http.latency_p95_s）：建議核准（告警 30 → 18；已確認事故 7/7 件還抓得到）")

    def test_not_recommended_lists_lost(self):
        r = {"weeks": 4, "series": ["x"], "anomalies_before": 30, "anomalies_after": 2, "incidents_total": 2, "incidents_caught_before": 2,
             "incidents_caught_after": 1, "incidents_lost": ["i-1"], "missed_total": 1, "missed_caught_after": 0,
             "recommended": False, "reason": "會漏掉 1 件已確認的事故：mes-api 變慢"}
        s = B.format_backtest(r)
        self.assertIn("不建議（會漏掉 1 件已確認的事故：mes-api 變慢）", s)

    def test_no_series(self):
        self.assertEqual(B.format_backtest({"recommended": False, "reason": "沒有序列符合 x"}), "回測：沒有序列符合 x")


if __name__ == "__main__":
    unittest.main()


import pandas as pd


class Timezones(unittest.TestCase):
    """docs/pdca/phase5-learning-loop/2026-10-fix-critical.md 2：incident add 存的時間沒有時區，跟有時區的異常時間比不能丟 TypeError。"""
    df = pd.DataFrame({"ts": [pd.Timestamp("2026-10-05T02:05:00Z"), pd.Timestamp("2026-10-05T09:00:00Z")], "_series": ["x", "x"], "_svc": ["mes-api", "pay"]})

    def test_naive_incident_time_is_local(self):
        # 事故寫「10-05 10:00～10:20」沒時區 → 當台北時間 = 02:00～02:20 UTC → 抓到 02:05 的那筆
        self.assertTrue(B.caught(self.df, ["mes-api"], "2026-10-05 10:00", "2026-10-05 10:20", "Asia/Taipei"))
        # 當成 UTC 的話 10:00～10:20Z 沒有 mes-api 的異常
        self.assertFalse(B.caught(self.df, ["mes-api"], "2026-10-05 10:00", "2026-10-05 10:20", "UTC"))

    def test_aware_time_kept(self):
        self.assertTrue(B.caught(self.df, ["mes-api"], "2026-10-05T02:00:00+00:00", "2026-10-05T02:20:00+00:00", "Asia/Taipei"))
        self.assertEqual(B._ts("2026-10-05T02:00:00+00:00", "Asia/Taipei").tzname(), "UTC")

    def test_empty_df(self):
        self.assertFalse(B.caught(self.df.iloc[0:0], None, "2026-10-05 10:00", "2026-10-05 10:20"))


class CountingES:
    """數 search／req 各被叫幾次，回傳帶著呼叫序號，看得出是不是同一次的結果。"""
    def __init__(self):
        self.calls = []

    def search(self, index, body):
        self.calls.append(("search", index, body))
        return {"n": len(self.calls)}

    def req(self, method, path, body=None, ok=(200, 201)):
        self.calls.append((method, path, body))
        return {"n": len(self.calls)}

    def bulk_index(self, index, docs):
        self.calls.append(("bulk", index, docs))


class CachedES(unittest.TestCase):
    """docs/pdca/phase5-learning-loop/2026-10-fix-critical.md Act「回測重複查 ES」：before／after 讀同一批資料，同一個查詢只打一次。"""
    def setUp(self):
        self.raw = CountingES()
        self.es = B._CachedES(self.raw)

    def test_same_search_body_once(self):
        body = {"query": {"range": {"@timestamp": {"gte": "2026-10-01"}}}, "size": 0}
        a = self.es.req("POST", "/idx/_search?ignore_unavailable=true", body)
        b = self.es.req("POST", "/idx/_search?ignore_unavailable=true", {"size": 0, "query": {"range": {"@timestamp": {"gte": "2026-10-01"}}}})
        self.assertIs(a, b)                       # key 順序不同也算同一個查詢
        self.assertEqual(len(self.raw.calls), 1)
        self.es.req("POST", "/idx/_search?ignore_unavailable=true", {**body, "size": 1})
        self.assertEqual(len(self.raw.calls), 2)  # body 不同就再查
        self.es.req("POST", "/other/_search?ignore_unavailable=true", body)
        self.assertEqual(len(self.raw.calls), 3)  # path 不同也再查

    def test_search_method_cached_too(self):
        body = {"size": 1000, "query": {"match_all": {}}}
        self.assertIs(self.es.search("aiops-incidents", body), self.es.search("aiops-incidents", dict(body)))
        self.assertEqual(len(self.raw.calls), 1)

    def test_writes_pass_through(self):
        for _ in range(2):
            self.es.req("PUT", "/aiops-tuning/_doc/x", {"a": 1})
            self.es.req("POST", "/aiops-tuning/_update/x?refresh=true", {"doc": {"a": 1}})
            self.es.req("DELETE", "/aiops-tuning/_doc/x", ok=(200, 404))
            self.es.bulk_index("aiops-anomalies", [("1", {})])
        self.assertEqual(len(self.raw.calls), 8)
        self.assertEqual(self.raw.calls[0], ("PUT", "/aiops-tuning/_doc/x", {"a": 1}))

    def test_run_backtest_uses_one_wrapper_for_both_labels(self):
        """detect_series 的每次呼叫（before、after、每一天）拿到的都是同一個包住真 ES 的物件。"""
        from unittest import mock
        seen = []

        def fake_detect(es, *a, **kw):
            seen.append(es)
            return []
        cfg = {"series": [{"name": "x", "by": ["service"]}], "train": {"timezone": "Asia/Taipei"}}
        now = pd.Timestamp("2026-10-10T00:00:00Z")
        with mock.patch.object(B, "detect_series", fake_detect), \
             mock.patch.object(B, "load_adjustments", lambda es, cfg: []), \
             mock.patch.object(BaselineRepository, "load", lambda self, name, keys: pd.DataFrame({"service": ["mes-api"]})), \
             mock.patch.object(B, "_confirmed_incidents", lambda es, since: []), \
             mock.patch.object(B, "_missed", lambda es, since: []):
            r = B.run_backtest(Repos(self.raw), cfg, {"series": "x", "kind": "override", "z": 6}, weeks=1, now=now)
        self.assertEqual(len(seen), 2 * 8)        # 兩套設定 × 8 天（含頭尾）
        self.assertTrue(all(s is seen[0] for s in seen))
        self.assertIsInstance(seen[0].es, B._CachedES)       # repo 包著快取的連線
        self.assertIs(seen[0].es._es, self.raw)
        self.assertEqual((r["anomalies_before"], r["anomalies_after"]), (0, 0))

"""tests/fakes.MemoryRepos：不用 ES 也能跑 train → detect、rules、peer、相似事故（指令的 repo= 參數）。不連 ES。"""
from shared.config import validate
import contextlib
import io
import unittest
from argparse import Namespace
from unittest import mock

import numpy as np
import pandas as pd

import detect.detect_service as C
import detect.strategy.peer_compare as cmd_peer
import detect.strategy.threshold_rule as cmd_rules
from tests.fakes import MemoryRepos

T0 = pd.Timestamp("2026-10-05T00:00:00Z")      # 週一
BY, KEYS = ["service.name", "transaction.name"], ["service", "route"]


def history(days=15, per_hour=60, spike_at: pd.Timestamp | None = None):
    """一列一個請求、每小時 per_hour 個，錯誤率平常 2%；spike_at 那一小時錯誤率 60%。"""
    rnd = np.random.default_rng(1)
    out = []
    for h in range(days * 24):
        ts = T0 + pd.Timedelta(hours=h)
        rate = 0.6 if spike_at is not None and ts == spike_at else 0.02
        for k in range(per_hour):
            out.append({"@timestamp": (ts + pd.Timedelta(seconds=k * 50)).isoformat(), "service.name": "mes-api", "transaction.name": "GET /lots",
                        "event.outcome": "failure" if rnd.random() < rate else "success", "transaction.duration.us": int(rnd.lognormal(11.5, 0.3))})
    return pd.DataFrame(out)


CFG = {"interval": "1h", "history_weeks": 2, "adjustments_file": None, "calendar_file": None,
       "train": {"timezone": "Asia/Taipei", "min_weeks": 1, "min_days": 2, "min_points": 20, "min_slot_points": 1},
       "detect": {"z": 5, "rel_floor": 0.05}, "status": {"confirm_points": 1},
       "series": [{"name": "http.error_rate", "index": "metrics-apm.transaction.1m-*", "by": BY, "keys": KEYS,
                   "filter": {"term": {"event.outcome": "failure"}}, "direction": "up", "min_abs": 0.05,
                   "value": {"ratio": {"term": {"event.outcome": "failure"}}}},
                  {"name": "http.requests", "index": "metrics-apm.transaction.1m-*", "by": BY, "keys": KEYS, "value": "count", "direction": "both"}]}
# 錯誤率那條故意沒加 filter 到 value 之外（ratio 自己帶 query）；上面 filter 只是示範 filter 也走 shape
CFG["series"][0].pop("filter")


def quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = fn(*a)
    return rc, out.getvalue()


class MemoryReposTrainDetect(unittest.TestCase):
    def test_train_then_detect_finds_the_injected_spike(self):
        """程式直接給 DataFrame：指令的 repo= 參數。"""
        spike = T0 + pd.Timedelta(days=14, hours=10)
        be = MemoryRepos({"metrics-apm.transaction.1m-*": history(spike_at=spike)})
        with mock.patch.object(C, "ES", lambda: self.fail("不該連 ES")):
            rc, text = quiet(C.cmd_train, CFG, Namespace(end=(T0 + pd.Timedelta(days=14)).isoformat(), weeks=None, at=None), be)
            self.assertEqual(rc, 0)
            rc, text = quiet(C.cmd_detect, CFG, Namespace(at=(spike + pd.Timedelta(hours=1)).isoformat(), window="2h"), be)
        self.assertEqual(rc, 0)
        self.assertTrue(be.docs("aiops-baseline"))
        hits = [d for _, d in be.docs("aiops-anomalies") if d["series"] == "http.error_rate"]
        self.assertEqual([d["@timestamp"][:13] for d in hits], [spike.isoformat()[:13]])
        self.assertEqual(hits[0]["status"], "anomaly")
        self.assertEqual((hits[0]["dept"], hits[0]["group"]), (None, "unassigned"))
        self.assertEqual([d for i, d in be.docs("aiops-alerts") if i == "detect-status"][0]["missing_baseline"], 0)


class RulesPeerSimilarOnMemory(unittest.TestCase):
    def test_rules_increase_from_rows(self):
        r = pd.DataFrame([{"@timestamp": (T0 + pd.Timedelta(minutes=m)).isoformat(), "pod": p, "metrics.restarts": v}
                          for m, v in ((0, 1), (3, 1), (6, 3)) for p in ("p1",)] +
                         [{"@timestamp": (T0 + pd.Timedelta(minutes=m)).isoformat(), "pod": "p2", "metrics.restarts": 0} for m in (0, 3, 6)])
        cfg = validate({"interval": "1m", "lookback": "10m", "rules": [{"name": "restart", "title": "容器重啟", "type": "increase", "index": "k8s-pods-*", "field": "metrics.restarts",
                                                              "by": ["pod"], "op": ">=", "threshold": 1, "severity": "warning"}]}, "rules")
        be = MemoryRepos({"k8s-pods-*": r})
        rc, text = quiet(cmd_rules.cmd_rules, cfg, Namespace(at=(T0 + pd.Timedelta(minutes=7)).isoformat(), only=None), be)
        self.assertEqual(rc, 0)
        hits = [d for _, d in be.docs("aiops-anomalies")]
        self.assertEqual([d["keys"]["pod"] for d in hits], ["p1"])
        self.assertEqual(hits[0]["rule"], "restart")

    def test_peer_from_rows(self):
        r = pd.DataFrame([{"@timestamp": (T0 + pd.Timedelta(minutes=9, seconds=s)).isoformat(), "ns": "n", "deploy": "d", "pod": p, "cpu": c}
                          for p, c in (("a", 0.2), ("b", 0.2), ("c", 0.2), ("d", 1.5)) for s in range(3)])
        cfg = validate({"window": "10m", "fresh": "2m", "min_peers": 3, "checks": [
            {"name": "peer.pod_cpu", "title": "CPU 高", "index": "k8s-*", "by": ["ns", "deploy", "pod"], "value": {"avg": "cpu"},
             "direction": "up", "min_abs": 0.2, "min_ratio": 3}]}, "peer")
        be = MemoryRepos({"k8s-*": r})
        rc, _ = quiet(cmd_peer.cmd_peer, cfg, Namespace(at=(T0 + pd.Timedelta(minutes=10)).isoformat(), only=None), be)
        self.assertEqual(rc, 0)
        self.assertEqual([d["keys"]["pod"] for _, d in be.docs("aiops-anomalies")], ["d"])

    def test_similar_from_backend_incidents(self):
        import llm.similar as SIM
        fp = {"services": ["mes-api"], "routes": [], "series": ["http.error_rate"], "slow_segments": ["mes-api › SELECT WIP → oracle"],
              "error_origins": [], "patterns": []}
        inc = [("i-db", {"status": "confirmed", "title": "連線池耗盡", "root_cause_category": "db",
                         "fingerprint": {**fp, "series": ["http.error_rate", "http.latency_p95_s"]}}),
               ("i-draft", {"status": "proposed", "title": "還沒確認", "fingerprint": fp}),
               ("i-other", {"status": "confirmed", "title": "別的服務", "fingerprint": {"services": ["erp"], "series": ["x"]}})]
        be = MemoryRepos({}, incidents=inc)
        got = SIM.search(be, fp)
        self.assertEqual([g["incident_id"] for g in got], ["i-db"])
        self.assertIn("同一段變慢", "；".join(got[0]["why"]))
        self.assertEqual(SIM.search(be, fp, exclude_id="i-db"), [])

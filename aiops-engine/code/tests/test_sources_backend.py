"""sources/：不用 ES 也能跑 train → detect（memory、file 來源＋ Store）、rules、peer、相似事故；設定檔沒寫 source 時還是 ES。
不連 ES（docs/pdca/meta/2026-11-model-modules.md 的 Check）。"""
import contextlib
import io
import os
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

import numpy as np
import pandas as pd

import aiops.core.repository as sources
import aiops.service.detect.baseline_job as C
import aiops.service.detect.peer_job as cmd_peer
import aiops.service.detect.rules_job as cmd_rules
import aiops.service.detect.es_facade as DE
import aiops.core.repository.stores as stores

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


class MemoryBackendTrainDetect(unittest.TestCase):
    def test_train_then_detect_finds_the_injected_spike(self):
        """程式直接給 DataFrame：指令的 backend= 參數。"""
        spike = T0 + pd.Timedelta(days=14, hours=10)
        be = sources.MemoryBackend({"metrics-apm.transaction.1m-*": history(spike_at=spike)})
        with mock.patch.object(C, "ES", lambda: self.fail("不該連 ES")):
            rc, text = quiet(C.cmd_train, CFG, Namespace(end=(T0 + pd.Timedelta(days=14)).isoformat(), weeks=None, at=None), be)
            self.assertEqual(rc, 0)
            rc, text = quiet(C.cmd_detect, CFG, Namespace(at=(spike + pd.Timedelta(hours=1)).isoformat(), window="2h"), be)
        self.assertEqual(rc, 0)
        self.assertTrue(be.store.docs("aiops-baseline"))
        hits = [d for _, d in be.store.docs("aiops-anomalies") if d["series"] == "http.error_rate"]
        self.assertEqual([d["@timestamp"][:13] for d in hits], [spike.isoformat()[:13]])
        self.assertEqual(hits[0]["status"], "anomaly")
        self.assertEqual((hits[0]["dept"], hits[0]["group"]), (None, "unassigned"))
        self.assertEqual([d for i, d in be.store.docs("aiops-alerts") if i == "detect-status"][0]["missing_baseline"], 0)


class FileBackendAcrossRuns(unittest.TestCase):
    def test_csv_source_and_file_store_survive_a_second_process(self):
        spike = T0 + pd.Timedelta(days=14, hours=10)
        with tempfile.TemporaryDirectory() as d:
            csv = os.path.join(d, "tx.csv")
            history(spike_at=spike).to_csv(csv, index=False)
            cfg = {**CFG, "source": {"kind": "file", "format": "events", "files": {"metrics-apm.transaction.1m-*": csv}},
                   "store": {"kind": "file", "dir": os.path.join(d, "store")}}
            with mock.patch.object(C, "ES", lambda: self.fail("不該連 ES")):
                rc, _ = quiet(C.cmd_train, cfg, Namespace(end=(T0 + pd.Timedelta(days=14)).isoformat(), weeks=None, at=None))
                self.assertEqual(rc, 0)
                self.assertTrue(os.path.exists(os.path.join(d, "store", "aiops-baseline.ndjson")))
                # 第二次「行程」：open_backend 重開 FileStore，baseline 從檔案讀回來
                rc, text = quiet(C.cmd_detect, cfg, Namespace(at=(spike + pd.Timedelta(hours=1)).isoformat(), window="2h"))
            self.assertEqual(rc, 0)
            self.assertNotIn("沒有 baseline", text)
            st = stores.FileStore(os.path.join(d, "store"))
            hits = [x for _, x in st.docs("aiops-anomalies") if x["series"] == "http.error_rate"]
            self.assertEqual([x["@timestamp"][:13] for x in hits], [spike.isoformat()[:13]])

    def test_unknown_index_names_what_is_available(self):
        be = sources.FileBackend({"metrics-apm.transaction.1m-*": "nope.csv"})
        with self.assertRaises(KeyError) as cm:
            be.rows("k8s-pods-*", T0, T0)
        self.assertIn("metrics-apm.transaction.1m-*", str(cm.exception))


class OpenBackendTest(unittest.TestCase):
    def test_no_source_means_es(self):
        """沒寫 source（或 source、store 都是 es）：拿到讀寫都是 ES 的 ESBackend，裡面包著 es_factory 給的連線。"""
        sentinel = object()
        for cfg in ({"series": []}, {"source": {"kind": "es"}}, {"store": {"kind": "es"}}):
            be = sources.open_backend(cfg, lambda: sentinel)
            self.assertIsInstance(be, sources.ESBackend)
            self.assertIs(be.es, sentinel)
            self.assertEqual((be.kind, be.store.kind), ("es", "es"))

    def test_es_source_with_file_store_does_not_write_es(self):
        with tempfile.TemporaryDirectory() as d:
            be = sources.open_backend({"source": {"kind": "es"}, "store": {"kind": "file", "dir": d}}, lambda: object())
            self.assertEqual((be.kind, be.store.kind), ("es", "file"))

    def test_bad_kind(self):
        with self.assertRaises(ValueError):
            sources.open_backend({"source": {"kind": "kafka"}}, lambda: None)

    def test_baseline_columns_match_detector_es(self):
        self.assertEqual(stores.BASELINE_COLUMNS, DE.BASELINE_COLUMNS)
        self.assertEqual(stores._RENAMED, DE._RENAMED)


class HttpBackendTest(unittest.TestCase):
    def test_url_template_and_rows_key(self):
        seen = []

        def fetch(url):
            seen.append(url)
            return {"rows": [{"@timestamp": T0.isoformat(), "svc": "a", "v": 1}, {"@timestamp": T0.isoformat(), "svc": "a", "v": 3}]}
        be = sources.HttpBackend(url="http://x/rows?index={index}&start={start}&end={end}", fetch=fetch)
        df = be.series({"name": "s", "index": "m-*", "by": ["svc"], "value": {"avg": "v"}}, T0, T0 + pd.Timedelta(minutes=1), "1m")
        self.assertEqual(df["value"].tolist(), [2.0])
        self.assertIn("index=m-*", seen[0])
        self.assertIn("start=2026-10-05T00:00:00", seen[0])


class RulesPeerSimilarOnBackend(unittest.TestCase):
    def test_rules_increase_from_rows(self):
        r = pd.DataFrame([{"@timestamp": (T0 + pd.Timedelta(minutes=m)).isoformat(), "pod": p, "metrics.restarts": v}
                          for m, v in ((0, 1), (3, 1), (6, 3)) for p in ("p1",)] +
                         [{"@timestamp": (T0 + pd.Timedelta(minutes=m)).isoformat(), "pod": "p2", "metrics.restarts": 0} for m in (0, 3, 6)])
        cfg = {"interval": "1m", "lookback": "10m", "rules": [{"name": "restart", "title": "容器重啟", "type": "increase", "index": "k8s-pods-*", "field": "metrics.restarts",
                                                              "by": ["pod"], "op": ">=", "threshold": 1, "severity": "warning"}]}
        be = sources.MemoryBackend({"k8s-pods-*": r})
        rc, text = quiet(cmd_rules.cmd_rules, cfg, Namespace(at=(T0 + pd.Timedelta(minutes=7)).isoformat(), only=None), be)
        self.assertEqual(rc, 0)
        hits = [d for _, d in be.store.docs("aiops-anomalies")]
        self.assertEqual([d["keys"]["pod"] for d in hits], ["p1"])
        self.assertEqual(hits[0]["rule"], "restart")

    def test_peer_from_rows(self):
        r = pd.DataFrame([{"@timestamp": (T0 + pd.Timedelta(minutes=9, seconds=s)).isoformat(), "ns": "n", "deploy": "d", "pod": p, "cpu": c}
                          for p, c in (("a", 0.2), ("b", 0.2), ("c", 0.2), ("d", 1.5)) for s in range(3)])
        cfg = {"window": "10m", "fresh": "2m", "min_peers": 3, "checks": [
            {"name": "peer.pod_cpu", "title": "CPU 高", "index": "k8s-*", "by": ["ns", "deploy", "pod"], "value": {"avg": "cpu"},
             "direction": "up", "min_abs": 0.2, "min_ratio": 3}]}
        be = sources.MemoryBackend({"k8s-*": r})
        rc, _ = quiet(cmd_peer.cmd_peer, cfg, Namespace(at=(T0 + pd.Timedelta(minutes=10)).isoformat(), only=None), be)
        self.assertEqual(rc, 0)
        self.assertEqual([d["keys"]["pod"] for _, d in be.store.docs("aiops-anomalies")], ["d"])

    def test_similar_from_backend_incidents(self):
        import aiops.service.rca.similar as SIM
        fp = {"services": ["mes-api"], "routes": [], "series": ["http.error_rate"], "slow_segments": ["mes-api › SELECT WIP → oracle"],
              "error_origins": [], "patterns": []}
        inc = [("i-db", {"status": "confirmed", "title": "連線池耗盡", "root_cause_category": "db",
                         "fingerprint": {**fp, "series": ["http.error_rate", "http.latency_p95_s"]}}),
               ("i-draft", {"status": "proposed", "title": "還沒確認", "fingerprint": fp}),
               ("i-other", {"status": "confirmed", "title": "別的服務", "fingerprint": {"services": ["erp"], "series": ["x"]}})]
        be = sources.MemoryBackend({}, incidents=inc)
        got = SIM.search(be, fp)
        self.assertEqual([g["incident_id"] for g in got], ["i-db"])
        self.assertIn("同一段變慢", "；".join(got[0]["why"]))
        self.assertEqual(SIM.search(be, fp, exclude_id="i-db"), [])

"""detector/base.py：Anomaly.to_doc() 跟改成類別之前各指令手寫的 dict 一字不差；每個欄位都寫得出來；
Detector.run() 的順序（prepare → checks → evaluate → 一次 write → 回傳碼）、--only、一條壞掉只跳過那一條。
寫進去的異常存在測試用的 PostgreSQL（tests/pg.py）；原始資料用假的 ES。"""
import contextlib
import dataclasses
import io
import unittest
from types import SimpleNamespace
from argparse import Namespace
from unittest import mock

import pandas as pd

import detect.base as B
from detect.base import Anomaly, Detector
from tests.pg import fresh_db, needs_pg
from common.infra.repos import Repos
from common.scenario import load_scenario

SC = load_scenario()

SLOT = pd.Timestamp("2026-11-05T10:03:00Z")
DET = "2026-11-05T10:03:07+00:00"


class ToDoc(unittest.TestCase):
    """期望值是從 git show HEAD（改之前）的 cmd_rules／cmd_peer／cmd_quality／cmd_baseline／cmd_trace 抄下來的 dict。"""

    def check(self, a: Anomaly, expected: dict, detected_at=None):
        got = a.to_doc(detected_at)
        self.assertEqual(got, expected)
        self.assertEqual(list(got), list(expected))     # 鍵的順序也一樣（_source 長得一樣）

    def test_rules_style(self):
        since = pd.Timestamp("2026-11-05T09:58:00Z")
        keys = {"pod": "p1", "service": "mes"}
        a = Anomaly(ts=SLOT, series="rule.restart", rule="restart", keys=keys, value=3, expected_high=1, level="rule",
                    direction=">=", severity="warning", message="容器重啟", since=since, detected_at=DET)
        self.check(a, {   # 舊 cmd_rules.py
            "@timestamp": SLOT.isoformat(), "series": "rule.restart", "rule": "restart", "keys": keys,
            "value": 3, "median": None, "expected_low": None, "expected_high": 1,
            "z": None, "level": "rule", "direction": ">=", "severity": "warning",
            "message": "容器重啟", "since": since.isoformat(), "detected_at": DET})

    def test_peer_style(self):
        k = {"ns": "n", "deploy": "d", "pod": "d-1"}
        a = Anomaly(ts=SLOT, series="peer.pod_cpu", rule="peer.pod_cpu", keys=k, value=1.5, median=0.2, z=9.1, level="peer",
                    direction="up", severity="warning", message="CPU 高", detected_at=DET)
        self.check(a, {   # 舊 cmd_peer.py
            "@timestamp": SLOT.isoformat(), "series": "peer.pod_cpu", "rule": "peer.pod_cpu", "keys": k,
            "value": 1.5, "median": 0.2, "expected_low": None, "expected_high": None, "z": 9.1,
            "level": "peer", "direction": "up", "severity": "warning",
            "message": "CPU 高", "detected_at": DET})

    def test_quality_style(self):
        keys = {"namespace": "mes", "field": "service.name"}
        a = Anomaly(ts=SLOT, series="quality.missing", rule="quality.missing", keys=keys, value=0.3, median=0.01, level="quality",
                    direction="up", severity="critical", message="缺", detected_at=DET)
        self.check(a, {   # 舊 cmd_quality.py
            "@timestamp": SLOT.isoformat(), "series": "quality.missing", "rule": "quality.missing", "keys": keys,
            "value": 0.3, "median": 0.01, "expected_low": None, "expected_high": None, "z": None,
            "level": "quality", "direction": "up", "severity": "critical", "message": "缺",
            "detected_at": DET})

    def test_baseline_style(self):
        keys = {"service": "mes"}
        a = Anomaly(ts=SLOT, series="http.error_rate", keys=keys, value=0.5, median=0.01, expected_low=0.0, expected_high=0.05, z=12.0,
                    level="hour", direction="up", detected_at=DET, resolution=None, method="mad", baseline_source="self",
                    confidence="medium", status="anomaly", streak=2)
        self.check(a, {   # 舊 cmd_baseline.save_anomalies：resolution None 時鍵還在；streak 有才寫、p 沒有就沒有
            "@timestamp": SLOT.isoformat(), "series": "http.error_rate", "keys": keys, "value": 0.5,
            "median": 0.01, "expected_low": 0.0, "expected_high": 0.05,
            "z": 12.0, "level": "hour", "direction": "up", "detected_at": DET,
            "resolution": None, "method": "mad", "baseline_source": "self",
            "confidence": "medium", "status": "anomaly", "streak": 2})
        a.resolution, a.p, a.streak = "5m", 1e-9, None
        d = a.to_doc()
        self.assertEqual((d["resolution"], d["p"]), ("5m", 1e-9))
        self.assertNotIn("streak", d)

    def test_trace_new_style_and_detected_at_argument(self):
        keys = {"entry.service": "a", "entry.name": "GET /x", "segment": "a › SELECT X → mysql"}
        a = Anomaly(ts=SLOT, series="trace.segment.new", keys=keys, value=1.0, median=0.0, expected_low=0.0, expected_high=0.0,
                    level="rule", direction="up")
        self.check(a, {   # 舊 cmd_trace.py：沒有 rule／severity／message；detected_at 是整批共用、to_doc 時給
            "@timestamp": SLOT.isoformat(), "series": "trace.segment.new", "keys": keys,
            "value": 1.0, "median": 0.0, "expected_low": 0.0, "expected_high": 0.0, "z": None, "level": "rule",
            "direction": "up", "detected_at": DET}, detected_at=DET)
        self.assertTrue(a.to_doc()["detected_at"].startswith("20"))     # 沒給就是現在

    def test_default_id(self):
        from common.infra.es import doc_id
        a = Anomaly(ts=SLOT, series="rule.x", keys={"a": 1}, value=1, level="rule")
        self.assertEqual(a.id(), doc_id("rule.x", {"a": 1}, SLOT))


class AllFieldsWritten(unittest.TestCase):
    """Anomaly 是異常文件唯一的定義處：每個 dataclass 欄位都寫得出來（沒有定義了卻永遠不寫的欄位）。"""

    def test_every_field_is_written(self):
        full = Anomaly(ts=SLOT, series="s", keys={}, value=1, level="rule", median=1, expected_low=0, expected_high=2, z=1, direction="up",
                       detected_at=DET, rule="r", severity="warning", message="m", since=SLOT, resolution="5m", method="mad",
                       baseline_source="self", confidence="high", status="anomaly", streak=1, p=0.1)
        keys = set(full.to_doc())
        self.assertEqual({"@timestamp" if f.name == "ts" else f.name for f in dataclasses.fields(Anomaly)}, keys)


class FakeES:
    """原始資料（歸屬查詢）回空。"""
    def req(self, method, path, body=None, ok=None):
        return {"aggregations": {}}

    def search(self, index, body):
        return {"aggregations": {}}


class Toy(Detector):
    """兩個檢查，各回一個異常；evaluate 到名字是 bad 的就丟例外。"""
    level = "toy"

    def __init__(self, cfg, args, repo=None, log=None):
        super().__init__(cfg, args, repo)
        self.log = log if log is not None else []

    def prepare(self):
        super().prepare()
        self.log.append("prepare")

    def checks(self):
        self.log.append("checks")
        return self.config.checks

    def evaluate(self, c):
        self.log.append(f"evaluate:{c.name}")
        if c.name == "bad":
            raise RuntimeError("查詢壞了")
        return [Anomaly(ts=self.slot, series=c.name, rule=c.name, keys={"k": c.name}, value=1, level=self.level,
                        direction="up", severity="warning", message=f"{c.name} 超過")]

    def write(self, docs):
        self.log.append(f"write:{len(docs)}")
        super().write(docs)


class Tolerant(Toy):
    def on_error(self, c, exc):
        self.failed.append(c.name)
        self.log.append(f"error:{c.name}")


def run(cls, checks, only=None):
    log = []
    repo = Repos(FakeES(), SC, db=fresh_db())
    cfg = SimpleNamespace(interval="1m", checks=[SimpleNamespace(name=n) for n in checks])
    args = Namespace(at=pd.Timestamp("2026-11-05T10:03:20Z"), only=only)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = cls(cfg, args, repo, log).run()
    written = sorted(repo.anomalies._select(), key=lambda x: x[1]["series"])
    return rc, repo, written, log, out.getvalue()


@needs_pg
class Template(unittest.TestCase):
    def test_order_and_single_write(self):
        rc, be, written, log, text = run(Toy, ["a", "b"])
        self.assertEqual(rc, 0)
        self.assertEqual(log, ["prepare", "checks", "evaluate:a", "evaluate:b", "write:2"])
        self.assertEqual([d["series"] for _, d in written], ["a", "b"])
        self.assertEqual(written[0][1]["@timestamp"], "2026-11-05T10:03:00+00:00")       # now 取整到 interval
        self.assertEqual(written[0][0], Anomaly(ts=SLOT, series="a", keys={"k": "a"}, value=1, level="toy").id())
        self.assertIn("  [warning] a 超過\n", text)
        self.assertIn("11-05 18:03：2 個檢查，觸發 2 個", text)                           # 台北時間

    def test_only_filters(self):
        rc, _, written, log, text = run(Toy, ["a", "b"], only="b")
        self.assertEqual(log, ["prepare", "checks", "evaluate:b", "write:1"])
        self.assertEqual([d["series"] for _, d in written], ["b"])
        self.assertIn("：1 個檢查，觸發 1 個", text)

    def test_nothing_fired_does_not_write(self):
        class Quiet(Toy):
            def evaluate(self, c):
                return []
        rc, _, written, log, _ = run(Quiet, ["a"])
        self.assertEqual((rc, written), (0, []))
        self.assertNotIn("write:0", log)

    def test_default_on_error_raises(self):
        with self.assertRaises(RuntimeError):
            run(Toy, ["a", "bad", "c"])

    def test_tolerant_skips_only_the_broken_one(self):
        rc, _, written, log, _ = run(Tolerant, ["a", "bad", "c"])
        self.assertEqual(rc, 1)                                                           # 有失敗的回 1（rules 的行為）
        self.assertEqual(log, ["prepare", "checks", "evaluate:a", "evaluate:bad", "error:bad", "evaluate:c", "write:2"])
        self.assertEqual([d["series"] for _, d in written], ["a", "c"])


if __name__ == "__main__":
    unittest.main()

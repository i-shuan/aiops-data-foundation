"""detector prune（aiops-* 超過保留天數的文件每天清）的單元測試，不需要 ES。"""
import contextlib
import io
import unittest
from argparse import Namespace
from unittest import mock

import pandas as pd
import yaml

from detector import prune as P

NOW = pd.Timestamp("2026-11-01T03:30:00Z")
CFG = {"timezone": "Asia/Taipei", "indices": [
    {"index": "aiops-anomalies", "keep_days": 90},
    {"index": "aiops-alerts", "time_field": "resolved_at", "keep_days": 180, "filter": {"term": {"status": "resolved"}}},
]}


class FakeES:
    def __init__(self, counts):
        self.counts, self.calls = counts, []

    def req(self, method, path, body=None, ok=None):
        self.calls.append((method, path, body))
        index = path.split("/")[1]
        if "_count" in path:
            return {"count": self.counts.get(index, 0)}
        if "_delete_by_query" in path:
            return {"deleted": self.counts.get(index, 0), "failures": []}
        raise AssertionError(path)


def run(es, **kw):
    with mock.patch.object(P, "ES", lambda: es), contextlib.redirect_stdout(io.StringIO()) as out:
        rc = P.cmd_prune(CFG, Namespace(at="2026-11-01T03:30:00Z", **{"dry_run": False, **kw}))
    return rc, out.getvalue()


class Plan(unittest.TestCase):
    def test_cutoff_and_filter(self):
        a, al = P.plan(CFG, NOW)
        self.assertEqual((a["index"], a["time_field"]), ("aiops-anomalies", "@timestamp"))
        self.assertEqual(a["query"], {"bool": {"filter": [{"range": {"@timestamp": {"lt": "2026-08-03T03:30:00+00:00"}}}]}})   # 90 天前
        self.assertEqual(al["query"]["bool"]["filter"], [{"range": {"resolved_at": {"lt": "2026-05-05T03:30:00+00:00"}}},
                                                          {"term": {"status": "resolved"}}])                              # 180 天前、只刪已結案的

    def test_shipped_config_only_touches_derived_indices(self):
        with open("configs/baseline/prune.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        names = {i["index"] for i in cfg["indices"]}
        self.assertEqual(names, {"aiops-anomalies", "aiops-trace-segments", "aiops-alerts"})
        for forbidden in ("aiops-incidents", "aiops-feedback", "aiops-tuning", "aiops-baseline"):
            self.assertNotIn(forbidden, names)
        alerts = next(i for i in cfg["indices"] if i["index"] == "aiops-alerts")
        self.assertEqual((alerts["time_field"], alerts["filter"]), ("resolved_at", {"term": {"status": "resolved"}}))


class Run(unittest.TestCase):
    def test_deletes_by_query_and_reports(self):
        es = FakeES({"aiops-anomalies": 12, "aiops-alerts": 3})
        rc, out = run(es)
        self.assertEqual(rc, 0)
        dels = [(p, b) for m, p, b in es.calls if "_delete_by_query" in p]
        self.assertEqual([p.split("/")[1] for p, _ in dels], ["aiops-anomalies", "aiops-alerts"])
        self.assertIn("conflicts=proceed", dels[0][0])
        self.assertIn("refresh=true", dels[0][0])
        self.assertEqual(dels[1][1]["query"]["bool"]["filter"][1], {"term": {"status": "resolved"}})
        self.assertIn("刪了 12 筆", out)
        self.assertIn("共 15 筆", out)

    def test_dry_run_only_counts(self):
        es = FakeES({"aiops-anomalies": 12})
        rc, out = run(es, dry_run=True)
        self.assertFalse(any("_delete_by_query" in p for _, p, _ in es.calls))
        self.assertIn("沒刪（--dry-run）", out)
        self.assertIn("共 12 筆要刪", out)

    def test_nothing_to_delete_skips_delete(self):
        es = FakeES({})
        run(es)
        self.assertFalse(any("_delete_by_query" in p for _, p, _ in es.calls))


if __name__ == "__main__":
    unittest.main()

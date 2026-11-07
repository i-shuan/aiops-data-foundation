"""detector prune（engine 自己的資料超過保留天數的每天清）：對真的 PostgreSQL（tests/pg.py）跑。為了確認：
1. 保留天數算對（cutoff）；附的設定只清異常、trace 每段的統計、告警，不碰事故、回饋、調整、基準線。
2. 異常、統計依自己的時間刪；告警只刪已結案、結案時間早於 cutoff 的（開著的件、狀態文件不刪）。
3. --dry-run 只數不刪。"""
import contextlib
import io
import unittest
from argparse import Namespace

import pandas as pd
import yaml

import detect.housekeeping as P
from common.config import validate
from common.infra.repos import Repos
from common.scenario import load_scenario
from tests.pg import fresh_db, needs_pg

NOW = pd.Timestamp("2026-11-01T03:30:00Z")
CFG = validate({"timezone": "Asia/Taipei", "tables": [{"table": "anomalies", "keep_days": 90}, {"table": "alerts", "keep_days": 180}]}, "prune")


class Plan(unittest.TestCase):
    def test_cutoff(self):
        a, al = P.plan(CFG, NOW)
        self.assertEqual((a["table"], a["cutoff"].isoformat()), ("anomalies", "2026-08-03T03:30:00+00:00"))    # 90 天前
        self.assertEqual((al["table"], al["cutoff"].isoformat()), ("alerts", "2026-05-05T03:30:00+00:00"))     # 180 天前

    def test_shipped_config_only_touches_derived_data(self):
        with open("configs/baseline/prune.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self.assertEqual({t["table"] for t in cfg["tables"]}, {"anomalies", "segments", "alerts"})
        validate(cfg, "prune")

    def test_unknown_table_is_rejected(self):
        with self.assertRaises(Exception):
            validate({"tables": [{"table": "incidents", "keep_days": 1}]}, "prune")


@needs_pg
class Run(unittest.TestCase):
    def setUp(self):
        self.repo = Repos(None, load_scenario(), db=fresh_db())
        old, new = "2026-07-01T00:00:00+00:00", "2026-10-30T00:00:00+00:00"
        self.repo.anomalies.bulk([("a-old", {"@timestamp": old, "series": "s"}), ("a-new", {"@timestamp": new, "series": "s"})])
        self.repo.alerts.bulk([
            ("al-old", {"@timestamp": old, "status": "resolved", "resolved_at": "2026-04-01T00:00:00+00:00"}),
            ("al-recent", {"@timestamp": new, "status": "resolved", "resolved_at": "2026-10-01T00:00:00+00:00"}),
            ("al-open", {"@timestamp": old, "status": "open", "started_at": "2026-01-01T00:00:00+00:00"}),
            ("detect-status", {"@timestamp": old, "status": "ok"})])

    def run_prune(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = P.cmd_prune(CFG, Namespace(at=NOW, **{"dry_run": False, **kw}), self.repo)
        return rc, out.getvalue()

    def test_deletes_old_and_keeps_open(self):
        rc, out = self.run_prune()
        self.assertEqual(rc, 0)
        self.assertEqual(self.repo.anomalies.get_many(["a-old", "a-new"]).keys(), {"a-new"})
        self.assertEqual({i for i, _ in self.repo.alerts._select()}, {"al-recent", "al-open", "detect-status"})
        self.assertIn("anomalies：刪了 1 筆", out)
        self.assertIn("共 2 筆", out)

    def test_dry_run_only_counts(self):
        rc, out = self.run_prune(dry_run=True)
        self.assertIn("沒刪（--dry-run）", out)
        self.assertIn("共 2 筆要刪", out)
        self.assertEqual(len(self.repo.anomalies.get_many(["a-old", "a-new"])), 2)


if __name__ == "__main__":
    unittest.main()

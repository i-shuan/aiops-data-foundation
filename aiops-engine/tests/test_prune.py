"""detector prune（engine 自己的資料超過保留天數的每天清）：對真的 PostgreSQL（tests/pg.py）跑。為了確認：
1. 保留天數算對（cutoff）；附的設定只清異常、trace 每段的統計、告警，不碰事故、回饋、調整、基準線。
2. 異常、統計依自己的時間刪；告警只刪已結案、結案時間早於 cutoff 的（開著的件、狀態文件不刪）。
3. --dry-run 只數不刪。
4. 歸檔（docs/pdca/meta/2026-11-prune-archive.md）：刪之前一天一個 .jsonl.gz、分批也不漏不重；restore 讀回跟原本一樣（id、ts、文件）；
   資料夾寫不進去一筆都不刪；AIOPS_ARCHIVE_DIR 蓋過設定、off 不歸檔；寫到一半當掉的檔讀得到前面的；歸檔檔案超過天數刪掉。"""
import contextlib
import gzip
import io
import os
import tempfile
import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd
import yaml

import common.infra.archive as ARC
import common.infra.prune as P
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


def archived(cfg_archive: dict) -> object:
    return validate({"timezone": "Asia/Taipei", "archive": cfg_archive,
                     "tables": [{"table": "anomalies", "keep_days": 90}, {"table": "alerts", "keep_days": 180}]}, "prune")


@needs_pg
class Archive(unittest.TestCase):
    def setUp(self):
        self.repo = Repos(None, load_scenario(), db=fresh_db())
        self.dir = tempfile.mkdtemp(dir="/tmp")
        # 7/1、7/2（台北時間）各 3 筆舊的；7/1 23:30 UTC 是台北 7/2 07:30 → 算 7/2
        self.old = [(f"a{i}", {"@timestamp": t, "series": "s", "value": i, "keys": {"service": "中文-api"}})
                    for i, t in enumerate(["2026-07-01T01:00:00+00:00", "2026-07-01T02:00:00+00:00", "2026-07-01T03:00:00+00:00",
                                           "2026-07-01T23:30:00+00:00", "2026-07-02T05:00:00+00:00", "2026-07-02T06:00:00+00:00"])]
        self.repo.anomalies.bulk(self.old + [("new", {"@timestamp": "2026-10-30T00:00:00+00:00", "series": "s"})])
        self.repo.alerts.bulk([("al-old", {"@timestamp": "2026-03-01T00:00:00+00:00", "status": "resolved", "resolved_at": "2026-04-01T00:00:00+00:00"}),
                               ("al-open", {"@timestamp": "2026-03-01T00:00:00+00:00", "status": "open"})])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_prune(self, cfg, env=None):
        with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = P.cmd_prune(cfg, Namespace(at=NOW, dry_run=False), self.repo)
        return rc, out.getvalue()

    def lines(self, table):
        return [r for f in ARC.files([Path(self.dir) / table]) for r in ARC.read(f)]

    def test_archive_then_delete_by_day_in_batches(self):
        rc, out = self.run_prune(archived({"dir": self.dir, "batch": 2}))         # 6 筆、一次 2 筆：分 3 批
        self.assertEqual(rc, 0, out)
        names = sorted(p.name for p in ARC.files([self.dir]))
        self.assertEqual(names, ["aiops_alerts-2026-03-01.jsonl.gz", "aiops_anomalies-2026-07-01.jsonl.gz", "aiops_anomalies-2026-07-02.jsonl.gz"])
        got = self.lines("aiops_anomalies")
        self.assertEqual(sorted(r["id"] for r in got), [i for i, _ in self.old])   # 不漏、不重
        self.assertEqual(self.repo.anomalies.get_many([i for i, _ in self.old] + ["new"]).keys(), {"new"})
        self.assertEqual({i for i, _ in self.repo.alerts._select()}, {"al-open"})
        self.assertIn("歸檔並刪了 6 筆", out)
        with gzip.open(Path(self.dir) / "aiops_anomalies" / "2026" / "aiops_anomalies-2026-07-02.jsonl.gz", "rt", encoding="utf-8") as f:
            self.assertIn("中文-api", f.read())                                  # zcat 看得懂（不是 \u 跳脫）

    def test_restore_brings_back_the_same_rows(self):
        from typer.testing import CliRunner
        import interface.cli.db as DBCLI
        before = {i: self.repo.anomalies.get(i) for i, _ in self.old}
        self.run_prune(archived({"dir": self.dir}))
        self.assertEqual(self.repo.anomalies.get_many(list(before)), {})
        with mock.patch.object(DBCLI, "Repos", lambda *a, **k: self.repo):
            res = CliRunner().invoke(DBCLI.db, ["restore", self.dir, "--table", "aiops_anomalies"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("aiops_anomalies", res.output)
        self.assertEqual({i: self.repo.anomalies.get(i) for i in before}, before)
        self.assertEqual(self.repo.anomalies.count("ts < '2026-08-01'"), 6)         # ts 照原本的（依時間查得到）
        self.assertFalse(self.repo.alerts.get("al-old"))                            # --table 只讀回異常

    def test_unwritable_dir_deletes_nothing(self):
        blocker = Path(self.dir) / "file"
        blocker.write_text("x")
        rc, out = self.run_prune(archived({"dir": str(blocker / "sub")}))          # 路徑底下是檔案：建不了資料夾
        self.assertEqual(rc, 1)
        self.assertIn("一筆都沒刪", out)
        self.assertEqual(len(self.repo.anomalies.get_many([i for i, _ in self.old])), 6)

    def test_write_failure_mid_way_keeps_that_batch(self):
        with mock.patch.object(ARC, "write", side_effect=OSError("No space left on device")):
            rc, out = self.run_prune(archived({"dir": self.dir}))
        self.assertEqual(rc, 1)
        self.assertIn("No space left", out)
        self.assertEqual(len(self.repo.anomalies.get_many([i for i, _ in self.old])), 6)

    def test_env_overrides_and_off(self):
        other = tempfile.mkdtemp(dir=self.dir)
        rc, _ = self.run_prune(CFG, {"AIOPS_ARCHIVE_DIR": other})                    # 設定沒寫 archive，環境變數開
        self.assertEqual(rc, 0)
        self.assertEqual(len(ARC.files([other])), 3)
        self.repo.anomalies.bulk(self.old)
        rc, out = self.run_prune(archived({"dir": self.dir + "/nope"}), {"AIOPS_ARCHIVE_DIR": "off"})
        self.assertIn("anomalies：刪了 6 筆", out)
        self.assertFalse(os.path.exists(self.dir + "/nope"))


class ArchiveFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)

    def test_append_and_truncated_tail(self):
        rows = lambda ids: [{"id": i, "ts": pd.Timestamp("2026-07-01T01:00:00Z").to_pydatetime(), "doc": {"v": i}} for i in ids]
        (p,) = ARC.write(self.dir, "t", rows(["a", "b"]), "UTC")
        ARC.write(self.dir, "t", rows(["c"]), "UTC")                               # 同一天再來：接在後面
        self.assertEqual([r["id"] for r in ARC.read(p)], ["a", "b", "c"])
        whole = p.read_bytes()
        good = len(whole)
        ARC.write(self.dir, "t", rows(["d", "e"]), "UTC")
        p.write_bytes(p.read_bytes()[: good + 15])                                  # 第三段寫到一半當掉
        self.assertEqual([r["id"] for r in ARC.read(p)], ["a", "b", "c"])

    def test_expire_by_file_date(self):
        for d in ("2025-10-01", "2026-06-01"):
            ARC.write(self.dir, "t", [{"id": "x", "ts": pd.Timestamp(d + "T00:00:00Z").to_pydatetime(), "doc": {}}], "UTC")
        gone = ARC.expire(self.dir, 365, date(2026, 11, 1))
        self.assertEqual([p.name for p in gone], ["t-2025-10-01.jsonl.gz"])
        self.assertEqual([p.name for p in ARC.files([self.dir])], ["t-2026-06-01.jsonl.gz"])


if __name__ == "__main__":
    unittest.main()

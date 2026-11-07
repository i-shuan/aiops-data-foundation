"""detector prune（engine 自己的資料超過保留天數的每天清）：對真的 PostgreSQL（tests/pg.py）跑。為了確認：
1. 保留天數算對（cutoff）；附的設定只清異常、trace 每段的統計、告警，不碰事故、回饋、調整、基準線。
2. 異常、統計依自己的時間刪；告警只刪已結案、結案時間早於 cutoff 的（開著的件、狀態文件不刪）。
3. --dry-run 只數不刪。
4. 打包（docs/pdca/meta/2026-11-prune-pack.md）：超過保留天數的資料一天打包成一筆 gzip 再刪細筆，不漏不重；restore 讀回跟原本一樣（id、ts、文件）；
   打包寫不進去那一批不刪；pack: null 直接刪；打包的資料超過天數刪掉。"""
import contextlib
import io
import unittest
from argparse import Namespace
from unittest import mock

import pandas as pd
import yaml

import common.infra.pack as PACK
import common.infra.prune as P
from common.config import validate
from common.infra.repos import Repos
from common.scenario import load_scenario
from tests.pg import fresh_db, needs_pg

NOW = pd.Timestamp("2026-11-01T03:30:00Z")
CFG = validate({"timezone": "Asia/Taipei", "pack": None, "tables": [{"table": "anomalies", "keep_days": 90}, {"table": "alerts", "keep_days": 180}]}, "prune")


class Plan(unittest.TestCase):
    def test_cutoff(self):
        a, al = P.plan(CFG, NOW)
        self.assertEqual((a["table"], a["cutoff"].isoformat()), ("anomalies", "2026-08-03T03:30:00+00:00"))    # 90 天前
        self.assertEqual((al["table"], al["cutoff"].isoformat()), ("alerts", "2026-05-05T03:30:00+00:00"))     # 180 天前

    def test_shipped_config_only_touches_derived_data(self):
        with open("configs/baseline/prune.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self.assertEqual({t["table"] for t in cfg["tables"]}, {"anomalies", "alerts"})
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
        self.assertIn("沒動（--dry-run）", out)
        self.assertIn("共 2 筆要刪", out)
        self.assertEqual(len(self.repo.anomalies.get_many(["a-old", "a-new"])), 2)


def packed(pack) -> object:
    return validate({"timezone": "Asia/Taipei", "pack": pack,
                     "tables": [{"table": "anomalies", "keep_days": 90}, {"table": "alerts", "keep_days": 180}]}, "prune")


@needs_pg
class Pack(unittest.TestCase):
    def setUp(self):
        self.repo = Repos(None, load_scenario(), db=fresh_db())
        # 台北時間 7/1 有 3 筆；UTC 7/1 23:30 是台北 7/2 07:30 → 算 7/2
        self.old = [(f"a{i}", {"@timestamp": t, "series": "s", "value": i, "keys": {"service": "中文-api"}})
                    for i, t in enumerate(["2026-07-01T01:00:00+00:00", "2026-07-01T02:00:00+00:00", "2026-07-01T03:00:00+00:00",
                                           "2026-07-01T23:30:00+00:00", "2026-07-02T05:00:00+00:00", "2026-07-02T06:00:00+00:00"])]
        self.repo.anomalies.bulk(self.old + [("new", {"@timestamp": "2026-10-30T00:00:00+00:00", "series": "s"})])
        self.repo.alerts.bulk([("al-old", {"@timestamp": "2026-03-01T00:00:00+00:00", "status": "resolved", "resolved_at": "2026-04-01T00:00:00+00:00"}),
                               ("al-open", {"@timestamp": "2026-03-01T00:00:00+00:00", "status": "open"})])

    def run_prune(self, cfg):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = P.cmd_prune(cfg, Namespace(at=NOW, dry_run=False), self.repo)
        return rc, out.getvalue()

    def rows(self, table):
        return [r for pk in self.repo.packed.packs(table, data=True) for r in PACK.unpack(pk["data"])]

    def test_pack_then_delete_by_day_in_batches(self):
        rc, out = self.run_prune(packed({"batch": 2}))                              # 6 筆、一次 2 筆：分 3 批
        self.assertEqual(rc, 0, out)
        got = self.rows("aiops_anomalies")
        self.assertEqual(sorted(r[0] for r in got), [i for i, _ in self.old])      # 不漏、不重
        self.assertEqual({pk["day"] for pk in self.repo.packed.packs("aiops_anomalies")}, {"2026-07-01", "2026-07-02"})
        self.assertEqual(self.repo.anomalies.get_many([i for i, _ in self.old] + ["new"]).keys(), {"new"})
        self.assertEqual({i for i, _ in self.repo.alerts._select()}, {"al-open"})
        self.assertIn("打包並刪了 6 筆", out)
        self.assertEqual({r[0] for r in self.rows("aiops_alerts")}, {"al-old"})

    def test_restore_brings_back_the_same_rows(self):
        from typer.testing import CliRunner
        import interface.cli.db as DBCLI
        before = {i: self.repo.anomalies.get(i) for i, _ in self.old}
        self.run_prune(packed({}))
        self.assertEqual(self.repo.anomalies.get_many(list(before)), {})
        with mock.patch.object(DBCLI, "Repos", lambda *a, **k: self.repo):
            res = CliRunner().invoke(DBCLI.db, ["restore", "--table", "aiops_anomalies", "--from", "2026-07-02", "--to", "2026-07-02"])
            self.assertEqual(res.exit_code, 0, res.output)
            self.assertEqual(set(self.repo.anomalies.get_many(list(before))), {"a3", "a4", "a5"})      # 只讀回 7/2（台北）
            res = CliRunner().invoke(DBCLI.db, ["restore", "--table", "aiops_anomalies"])
        self.assertEqual({i: self.repo.anomalies.get(i) for i in before}, before)                       # 每筆文件相同
        self.assertEqual(self.repo.anomalies.count("ts < '2026-08-01'"), 6)                             # ts 照原本的
        self.assertFalse(self.repo.alerts.get("al-old"))                                                # --table 只讀回異常

    def test_write_failure_keeps_the_batch(self):
        with mock.patch.object(type(self.repo.packed), "add", side_effect=RuntimeError("disk full")):
            rc, out = self.run_prune(packed({}))
        self.assertEqual(rc, 1)
        self.assertIn("disk full", out)
        self.assertEqual(len(self.repo.anomalies.get_many([i for i, _ in self.old])), 6)

    def test_pack_null_deletes_directly(self):
        rc, out = self.run_prune(packed(None))
        self.assertIn("anomalies：刪了 6 筆", out)
        self.assertEqual(self.repo.packed.packs(), [])

    def test_packed_data_expires(self):
        self.run_prune(packed({"keep_days": 365}))
        self.assertEqual(len(self.repo.packed.packs("aiops_alerts")), 1)         # 2026-04-01 結案：離 NOW 7 個月，還在
        later = pd.Timestamp("2027-06-01T03:30:00Z")
        with contextlib.redirect_stdout(io.StringIO()):
            P.cmd_prune(packed({"keep_days": 365}), Namespace(at=later, dry_run=False), self.repo)
        self.assertEqual(self.repo.packed.packs("aiops_alerts"), [])             # 2026-03-01 的包超過 365 天

    def test_pack_shrinks(self):
        rows = [{"id": f"i{i}", "ts": None, "doc": {"kind": "segment", "segment": "cs-portal › GET /home", "time_ms": 2.0 + i, "requests": 60}}
                for i in range(2000)]
        import json
        raw = len(json.dumps([r["doc"] for r in rows]))
        self.assertLess(len(PACK.pack(rows)), raw / 5)
        self.assertIsInstance(PACK.pack(rows), bytes)


if __name__ == "__main__":
    unittest.main()

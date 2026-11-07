"""本機日誌（common/infra/journal.py；docs/pdca/meta/2026-11-pg-resilience.md）：寫資料庫前先記在本機一份，寫不進去的之後照順序補寫。
對真的 PostgreSQL（tests/pg.py）跑。為了確認：
1. 平常：每筆照樣寫進資料庫；寫完檔案清空；程式結束檔案刪掉。查詢、建表不記。
2. 資料庫寫不進去：工作照樣看到錯誤，但那一筆留在檔案；同一個程式下一次寫入先補它、照原本的順序。
3. 程式死了還有沒補的：別的程式認領（只有一個人認領得到）、照順序補寫；補寫的結果跟直接寫一樣（含 bytea、時間、jsonb）。
4. 寫到一半當掉的最後一行跳過；補到一半又寫不進去：檔案放回去，下次接著補。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import psycopg

from common.infra.pg import DB
from common.infra.repos import Repos
from common.scenario import load_scenario
from tests.pg import fresh_db, needs_pg

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
T0 = datetime(2026, 11, 7, 2, 0, tzinfo=timezone.utc)

# 另一個程式：開日誌、寫兩筆，但資料庫「掛了」（每次寫都失敗），然後結束：檔案留著
DIES = """
import sys; sys.path[:0] = ["src", "."]
import psycopg
from unittest import mock
from common.infra.pg import DB
from common.infra.repos import Repos
from common.scenario import load_scenario
db = DB(sys.argv[1])
repo = Repos(None, load_scenario(), db=db)
repo.state.ensure()
repo.anomalies.ensure()
with mock.patch.object(DB, "_run", side_effect=psycopg.OperationalError("db down")):
    for i, doc in enumerate([{"@timestamp": "2026-11-07T02:00:00+00:00", "v": 1}, {"@timestamp": "2026-11-07T02:00:00+00:00", "v": 2}]):
        try:
            repo.anomalies.bulk([("same", doc)])          # 同一個 id 寫兩次：補寫要照順序，最後是 v=2
        except psycopg.OperationalError:
            pass
"""


@needs_pg
class Journal(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        self.env = mock.patch.dict(os.environ, {"AIOPS_JOURNAL_DIR": self.dir})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.dsn = fresh_db().dsn

    def files(self):
        return sorted(Path(self.dir).glob("*.jsonl*"))

    def test_normal_writes_go_through_and_file_is_emptied(self):
        db = DB(self.dsn)
        repo = Repos(None, load_scenario(), db=db)
        repo.anomalies.bulk([("a", {"@timestamp": T0.isoformat(), "series": "s"})])
        repo.packed.add("aiops_anomalies", [{"id": "x", "ts": T0, "doc": {"v": 1}}], "UTC")     # bytea、時間
        self.assertEqual(repo.anomalies.get("a")["series"], "s")
        (f,) = self.files()
        self.assertEqual(f.read_text(), "")                                     # 都寫進去了：清空
        db.journal.close()
        self.assertEqual(self.files(), [])                                      # 程式結束：刪掉

    def test_failed_write_stays_and_is_replayed_first_in_order(self):
        db = DB(self.dsn)
        repo = Repos(None, load_scenario(), db=db)
        repo.anomalies.ensure()
        real = DB._apply
        with mock.patch.object(DB, "_apply", side_effect=psycopg.OperationalError("db down")):
            with self.assertRaises(psycopg.OperationalError):
                repo.anomalies.bulk([("a", {"@timestamp": T0.isoformat(), "v": 1})])
        (f,) = self.files()
        self.assertEqual(len(f.read_text().splitlines()), 1)                    # 留在檔案
        self.assertIsNone(repo.anomalies.get("a"))
        calls = []
        with mock.patch.object(DB, "_apply", lambda self, sql, p, many: calls.append(p) or real(self, sql, p, many)):
            repo.anomalies.bulk([("a", {"@timestamp": T0.isoformat(), "v": 2})])     # 資料庫回來：先補 v=1、再寫 v=2
        self.assertEqual(repo.anomalies.get("a")["v"], 2)
        self.assertEqual([json.loads(r[0][2])["v"] for r in calls if r and isinstance(r, list)], [1, 2])
        self.assertEqual(f.read_text(), "")

    def test_dead_process_leftovers_are_recovered_once_in_order(self):
        p = subprocess.run([sys.executable, "-c", DIES, self.dsn], cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        (left,) = self.files()
        self.assertEqual(len(left.read_text().splitlines()), 2)                 # 兩筆都沒寫進去，留著
        db = DB(self.dsn)
        repo = Repos(None, load_scenario(), db=db)
        self.assertIsNone(repo.anomalies.get("same"))
        self.assertEqual(db.recover_journal(), 2)
        self.assertEqual(repo.anomalies.get("same")["v"], 2)                    # 照順序：最後是 v=2
        self.assertEqual(self.files(), [])                                      # 補完刪掉
        self.assertEqual(DB(self.dsn).recover_journal(), 0)                     # 不會被補第二次

    def test_torn_last_line_and_partial_recovery(self):
        subprocess.run([sys.executable, "-c", DIES, self.dsn], cwd=ROOT, env=os.environ.copy(), check=True, capture_output=True)
        (left,) = self.files()
        left.write_text(left.read_text() + '{"sql": "INSERT INTO aio')          # 寫到一半當掉
        db = DB(self.dsn)
        real, n = DB._apply, []

        def second_fails(self_, sql, params, many):
            n.append(1)
            if len(n) == 2:
                raise psycopg.OperationalError("db down again")
            return real(self_, sql, params, many)

        with mock.patch.object(DB, "_apply", second_fails):
            with self.assertRaises(psycopg.OperationalError):
                db.recover_journal()
        (back,) = self.files()
        self.assertEqual(back.name, left.name)                                  # 放回去
        self.assertEqual(len(back.read_text().splitlines()), 2)                 # 剩第二筆＋壞掉的那行
        self.assertEqual(db.recover_journal(), 1)                               # 接著補第二筆，壞掉的那行跳過
        self.assertEqual(Repos(None, load_scenario(), db=db).anomalies.get("same")["v"], 2)

    def test_unwritable_journal_still_writes_db(self):
        os.chmod(self.dir, 0o500)                                                # 資料夾不能寫（PVC 權限錯、磁碟滿）
        self.addCleanup(os.chmod, self.dir, 0o700)
        if os.access(self.dir, os.W_OK):
            self.skipTest("root 不受權限限制")
        repo = Repos(None, load_scenario(), db=DB(self.dsn))
        repo.state.set("k", {"v": 1})                                            # 照樣寫進資料庫
        self.assertEqual(repo.state.get("k"), {"v": 1})

    def test_append_failure_mid_way_still_writes_db(self):
        db = DB(self.dsn)
        repo = Repos(None, load_scenario(), db=db)
        repo.state.set("a", {"v": 1})
        with mock.patch.object(type(db.journal), "_append", side_effect=OSError("No space left on device")):
            repo.state.set("b", {"v": 2})
        self.assertEqual(repo.state.get("b"), {"v": 2})

    def test_reads_and_ddl_not_journaled(self):
        db = DB(self.dsn)
        db.query("SELECT 1")
        Repos(None, load_scenario(), db=db).state.ensure(force=True)
        self.assertEqual(self.files(), [])


if __name__ == "__main__":
    unittest.main()

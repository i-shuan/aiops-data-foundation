"""測試用的 PostgreSQL：engine 自己的資料存 PostgreSQL 之後，存取類別的測試要一個真的資料庫（SQL 寫錯，假的抓不到）。

  from tests.pg import fresh_db, needs_pg
  @needs_pg
  class X(unittest.TestCase):
      def setUp(self):
          self.db = fresh_db()          # 一個全新的空資料庫（每次呼叫一個，互不影響）

找資料庫的順序：
  1. 環境變數 AIOPS_TEST_PG_DSN（已經有的 PostgreSQL，例如 CI 的 service；要有 CREATE DATABASE 的權限）
  2. 本機有 PostgreSQL 的程式（initdb、pg_ctl）：在暫存資料夾開一個只給這次測試用的，跑完自動關、刪掉
  都沒有：needs_pg 的測試略過（skip），不算失敗。
"""
from __future__ import annotations

import atexit
import glob
import itertools
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from common.infra.pg import DB

_state: dict = {}
_counter = itertools.count()


def _bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    hits = sorted(glob.glob(f"/usr/lib/postgresql/*/bin/{name}"))
    return hits[-1] if hits else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_private() -> str | None:
    initdb, pg_ctl = _bin("initdb"), _bin("pg_ctl")
    if not (initdb and pg_ctl):
        return None
    # root 跑測試時資料夾要給 postgres 使用者：放在大家都進得去的 /tmp（TMPDIR 可能是只有 root 能進的資料夾）
    root = tempfile.mkdtemp(prefix="aiops-pg-", dir="/tmp" if os.geteuid() == 0 and os.path.isdir("/tmp") else None)
    data = os.path.join(root, "data")
    as_user: list[str] = []
    if os.geteuid() == 0:                     # initdb 不給 root 跑：用 postgres 這個使用者
        if shutil.which("runuser") is None:
            return None
        shutil.chown(root, "postgres", "postgres")
        as_user = ["runuser", "-u", "postgres", "--"]
    port = _free_port()
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    subprocess.run(as_user + [initdb, "-D", data, "-U", "aiops", "--auth=trust", "-E", "UTF8", "--no-locale"], check=True, **quiet)
    subprocess.run(as_user + [pg_ctl, "start", "-D", data, "-l", os.path.join(root, "log"), "-w", "-o",
                              f"-p {port} -k {root} -c listen_addresses=127.0.0.1 -c fsync=off -c full_page_writes=off"], check=True, **quiet)

    def stop():
        subprocess.run(as_user + [pg_ctl, "-D", data, "-m", "immediate", "stop"], **quiet)
        shutil.rmtree(root, ignore_errors=True)
    atexit.register(stop)
    return f"postgresql://aiops@127.0.0.1:{port}/postgres"


def admin_dsn() -> str | None:
    """能建資料庫的那條連線；找不到 PostgreSQL 回 None（只找一次）。"""
    if "dsn" not in _state:
        try:
            _state["dsn"] = os.environ.get("AIOPS_TEST_PG_DSN") or _start_private()
        except (OSError, subprocess.CalledProcessError):
            _state["dsn"] = None
    return _state["dsn"]


def fresh_db() -> DB:
    """一個全新的空資料庫。"""
    dsn = admin_dsn()
    if not dsn:
        raise unittest.SkipTest("沒有 PostgreSQL（設 AIOPS_TEST_PG_DSN，或裝 postgresql）")
    name = f"t_{os.getpid()}_{next(_counter)}"
    admin = DB(dsn)
    admin.execute(f"CREATE DATABASE {name}")
    admin.close()
    base, _, _ = dsn.rpartition("/")
    return DB(f"{base}/{name}")


needs_pg = unittest.skipUnless(admin_dsn(), "沒有 PostgreSQL（設 AIOPS_TEST_PG_DSN，或裝 postgresql）")


def fresh_dsn() -> str:
    """一個全新的空資料庫的連線字串（給 mock.patch.dict(os.environ, {"AIOPS_PG_DSN": …})：指令自己建的 Repos 也連到它）。"""
    return fresh_db().dsn


def env(db: DB):
    """讓這段期間建的 DB()（指令裡的 Repos(ES(), load_scenario())）都連到測試的資料庫。"""
    return mock.patch.dict(os.environ, {"AIOPS_PG_DSN": db.dsn})

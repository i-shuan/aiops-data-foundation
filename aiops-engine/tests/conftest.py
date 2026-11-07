"""全部測試共用：engine 自己的資料存 PostgreSQL，指令裡自己建的 Repos(ES(), load_scenario()) 也要連得到資料庫。

有 PostgreSQL（tests/pg.py 找得到）時：整次測試開一個測試資料庫，每個測試都把 AIOPS_PG_DSN 指到它，
測試結束把 aiops_ 開頭的表清空（下一個測試從空的開始）。要自己一個乾淨資料庫的測試照舊用 tests.pg.fresh_db()。
沒有 PostgreSQL：什麼都不做（要資料庫的測試會因為 AIOPS_PG_DSN 沒設而失敗，或用 needs_pg 略過）。"""
from __future__ import annotations

import os
from unittest import mock

import pytest

import common.scenario.mapping as MAPPING
from tests.pg import admin_dsn, fresh_db


@pytest.fixture(scope="session")
def _shared_db():
    return fresh_db() if admin_dsn() else None


@pytest.fixture(autouse=True)
def _pg_env(_shared_db):
    if _shared_db is None:
        yield
        return
    with mock.patch.dict(os.environ, {"AIOPS_PG_DSN": _shared_db.dsn}):
        yield
    MAPPING._cache.clear()
    tables = [r["tablename"] for r in _shared_db.query("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'aiops%%'")]
    if tables:
        _shared_db.execute("TRUNCATE " + ", ".join(tables))

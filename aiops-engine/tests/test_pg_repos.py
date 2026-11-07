"""engine 自己的資料搬到 PostgreSQL 之後（docs/pdca/meta/2026-11-postgres-store.md），每個存取類別的查詢條件對真的資料庫跑一遍。
為了確認：原本 ES 查詢的語意（時間區間 [start, end)、term、terms、exists、regexp 整串比對、排序、只回找得到的）在 SQL 裡一樣；
同 id 再寫覆蓋、部分更新只合併頂層；NaN、datetime、numpy 數字寫得進 jsonb；表還不存在時讀是空的、不是錯。"""
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from common.infra.pg import has
from common.infra.repos import Repos
from common.scenario import load_scenario
from tests.pg import fresh_db, needs_pg

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


def at(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


@needs_pg
class Base(unittest.TestCase):
    def setUp(self):
        self.repo = Repos(None, load_scenario(), db=fresh_db())

    def test_empty_table_reads_empty(self):
        self.assertEqual(self.repo.anomalies.between(T0, T0 + timedelta(hours=1)), [])
        self.assertIsNone(self.repo.state.get("trace-merge"))
        self.assertEqual(self.repo.tuning.all(), [])

    def test_overwrite_patch_and_odd_values(self):
        a = self.repo.anomalies
        a.bulk([("x", {"@timestamp": at(0), "series": "s", "value": float("nan"), "z": np.float64(2.5), "n": np.int64(3), "d": T0})])
        got = a.get("x")
        self.assertEqual((got["value"], got["z"], got["n"], got["d"]), (None, 2.5, 3, T0.isoformat()))
        a.bulk([("x", {"@timestamp": at(1), "series": "s2"})])                    # 同 id：整筆覆蓋
        self.assertEqual(a.get("x"), {"@timestamp": at(1), "series": "s2"})
        a._patch("x", {"alert_id": "a1"})                                          # 部分更新：只加頂層欄位
        self.assertEqual(a.get("x"), {"@timestamp": at(1), "series": "s2", "alert_id": "a1"})
        with self.assertRaises(KeyError):
            a._patch("nope", {"a": 1})
        self.assertEqual(a.count(has("alert_id")), 1)


@needs_pg
class Anomalies(unittest.TestCase):
    def setUp(self):
        self.a = Repos(None, load_scenario(), db=fresh_db()).anomalies
        self.a.bulk([
            ("r1", {"@timestamp": at(0), "series": "rule.http.error_rate", "level": "rule", "keys": {"service.name": "mes-api"}}),
            ("b1", {"@timestamp": at(5), "series": "http.latency_p95_s", "level": "baseline", "median": 0.1, "keys": {"service": "mes-api"}}),
            ("b2", {"@timestamp": at(10), "series": "http.latency_p95_s", "level": "baseline", "median": None, "keys": {"service": "pay"}}),
            ("late", {"@timestamp": at(60), "series": "http.latency_p95_s", "level": "baseline", "keys": {"service": "mes-api"}})])

    def ids(self, rows):
        return [r["_id"] for r in rows]

    def test_window_is_half_open_and_sorted(self):
        self.assertEqual(self.ids(self.a.between(T0, T0 + timedelta(minutes=60))), ["r1", "b1", "b2"])     # 60 分那筆不算（< end）

    def test_service_levels_regex_median(self):
        end = T0 + timedelta(hours=2)
        keys = ("service", "service.name")
        self.assertEqual(self.ids(self.a.between(T0, end, service="mes-api", service_keys=keys)), ["r1", "b1", "late"])
        self.assertEqual(self.ids(self.a.between(T0, end, levels=["rule"])), ["r1"])
        self.assertEqual(self.ids(self.a.between(T0, end, series_regex="http\\..*")), ["b1", "b2", "late"])   # 整串比對：rule.http… 不算
        self.assertEqual(self.ids(self.a.between(T0, end, with_median=True)), ["b1"])                          # median 是 null 的不算
        self.assertEqual(self.a.between(T0, end, ["series"], levels=["rule"]), [{"series": "rule.http.error_rate", "_id": "r1"}])

    def test_get_many_and_status(self):
        self.a.bulk([("s1", {"@timestamp": at(1), "status": "anomaly", "streak": 2}), ("s2", {"@timestamp": at(1), "status": "warning"})])
        self.assertEqual(set(self.a.get_many(["r1", "gone"])), {"r1"})
        self.assertEqual(self.a.status_of(["s1", "s2", "gone"]), {"s1": {"status": "anomaly", "streak": 2}})


@needs_pg
class BaselineAlertsFeedback(unittest.TestCase):
    def setUp(self):
        self.repo = Repos(None, load_scenario(), db=fresh_db())

    def test_baseline_save_replaces_older(self):
        b = self.repo.baseline
        row = {"series": "s", "keys": {"service": "a"}, "dow": 0, "hour": 1, "level": "all", "median": 1.0, "mad": 0.1, "resolution": "5m", "method": "mad"}
        b.save("s", [("old", {**row, "trained_at": at(0)}), ("grp", {**row, "baseline_source": "group:g1", "trained_at": at(0)})], at(0), True)
        b.save("s", [("new", {**row, "hour": 2, "trained_at": at(60)})], at(60), True)
        df = b.load("s", ["service"])
        self.assertEqual(list(df["hour"]), [2])                                     # 上一次的（含組的）刪掉
        self.assertEqual(list(df["baseline_method"]), ["mad"])
        b.save("s", [], at(120), False)                                             # 這次查不到資料：不刪
        self.assertEqual(len(b.load("s", ["service"])), 1)
        b.bulk([("g", {**row, "baseline_source": "group:g1", "trained_at": at(60)})])
        self.assertEqual(list(b.load_group("s")["group"]), ["g1"])
        self.assertEqual(len(b.load("s", ["service"])), 1)                          # 組的不混進自己的

    def test_alerts(self):
        al = self.repo.alerts
        al.bulk([("o", {"@timestamp": at(0), "alert_id": "o", "category": "service", "service": "a", "status": "open", "started_at": at(0), "updated_at": at(5), "report": "長"}),
                 ("r", {"@timestamp": at(0), "alert_id": "r", "status": "resolved", "started_at": at(1), "updated_at": at(9), "resolved_at": at(9),
                        "anomaly_ids": ["x1", "x2"]}),
                 ("detect-status", {"@timestamp": at(0), "alert_id": "detect-status", "status": "ok"})])
        self.assertEqual([a.alert_id for a in al.open()], ["o"])
        self.assertEqual(al.resolved_anomaly_ids(T0), {"x1", "x2"})
        self.assertEqual(al.resolved_anomaly_ids(T0 + timedelta(minutes=10)), set())
        self.assertEqual([a["alert_id"] for a in al.list("all")], ["r", "o", "detect-status"])    # 新的在前，沒 updated_at 的最後
        self.assertNotIn("report", al.list("open")[0])
        self.assertEqual([a["_id"] for a in al.started_between(T0, T0 + timedelta(minutes=1))], ["o"])
        al.request_rca("o", at(3))
        self.assertTrue(al.get("o")["rca_requested"])

    def test_feedback(self):
        fb = self.repo.feedback
        fb.bulk([("1", {"@timestamp": at(0), "verdict": "true_positive", "alert_id": "a1"}),
                 ("2", {"@timestamp": at(5), "verdict": "missed"}),
                 ("3", {"@timestamp": at(9), "verdict": "missed", "alert_id": "a2"})])
        self.assertEqual([f["_id"] for f in fb.since(T0 + timedelta(minutes=1))], ["2", "3"])
        self.assertEqual([f["_id"] for f in fb.missed_since(T0)], ["2", "3"])
        self.assertEqual([f["_id"] for f in fb.missed_since(T0, without_alert=True)], ["2"])
        self.assertEqual([f["_id"] for f in fb.by_alert_ids(["a2", "a1", None])], ["1", "3"])
        self.assertEqual(fb.by_alert_ids([]), [])


@needs_pg
class IncidentsTuningState(unittest.TestCase):
    def setUp(self):
        self.repo = Repos(None, load_scenario(), db=fresh_db())

    def test_incidents_find(self):
        inc = self.repo.incidents
        inc.bulk([("c1", {"status": "confirmed", "start": at(0), "root_cause_category": "db", "fingerprint": {"services": ["a"]}}),
                  ("c2", {"status": "corrected", "start": at(60)}),
                  ("au", {"status": "auto", "start": at(30), "root_cause_category": "db"})])
        self.assertEqual([i for i, _ in inc.find(("confirmed", "corrected"))], ["c2", "c1"])           # 新的在前
        self.assertEqual([i for i, _ in inc.find(("confirmed", "corrected"), having=("fingerprint",))], ["c1"])
        self.assertEqual([i for i, _ in inc.find(("confirmed", "corrected"), since=T0 + timedelta(minutes=1))], ["c2"])
        self.assertEqual([i for i, _ in inc.find(("confirmed", "corrected"), exclude_id="c2")], ["c1"])
        self.assertEqual([i for i, _ in inc.similar({"services": ["a"]}, ("confirmed",))], ["c1"])
        self.assertEqual(inc.similar({}, ("confirmed",)), [])

    def test_tuning_and_state(self):
        t = self.repo.tuning
        t.put("b", {"kind": "override", "series": "s", "z": 7, "status": "proposed", "at": at(10)})
        t.put("a", {"kind": "override", "series": "s", "z": 6, "at": at(0)})
        self.assertEqual([x["id"] for x in t.all()], ["a", "b"])                   # 依 at 排
        self.assertEqual([x["id"] for x in t.proposed()], ["b"])
        t.patch("b", {"status": "active"})
        self.assertEqual(t.proposed(), [])
        t.delete("a")
        self.assertFalse(t.exists("a"))
        self.repo.state.set("trace-merge", {"last": at(5)})
        self.assertEqual(self.repo.state.get("trace-merge"), {"last": at(5)})


if __name__ == "__main__":
    unittest.main()


@needs_pg
class Connection(unittest.TestCase):
    """連線設定（common/config/settings.py）：連線名稱、查詢逾時真的套用；查詢超時是錯誤、不會卡住。"""

    def test_settings_are_applied(self):
        import os
        from unittest import mock
        from common.infra.pg import DB
        dsn = fresh_db().dsn
        with mock.patch.dict(os.environ, {"AIOPS_PG_APP_NAME": "aiops-test", "AIOPS_PG_STATEMENT_TIMEOUT": "1"}):
            db = DB(dsn)
            got = db.query("SELECT current_setting('application_name') AS a, current_setting('statement_timeout') AS t")[0]
            self.assertEqual((got["a"], got["t"]), ("aiops-test", "1s"))
            with self.assertRaises(Exception):
                db.query("SELECT pg_sleep(3)")


@needs_pg
class LeastPrivilege(unittest.TestCase):
    """DBA 只給讀寫權限（docs/pdca/meta/2026-11-pg-resilience.md）：表由 detector db init（有 CREATE 權限的帳號）先建好，
    engine 的帳號沒有 schema 的 CREATE 也要能讀寫；表沒建時錯誤講得出是權限。"""

    def setUp(self):
        from common.infra.pg import DB
        self.admin = fresh_db()
        self.dsn = self.admin.dsn
        self.role = f"dml_{id(self) % 100000}"
        self.admin.execute(f"CREATE ROLE {self.role}")
        self.addCleanup(lambda: (self.admin.execute(f"DROP OWNED BY {self.role}"), self.admin.execute(f"DROP ROLE {self.role}")))
        self.admin.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        self.admin.execute(f"GRANT USAGE ON SCHEMA public TO {self.role}")
        self.DB = DB

    def as_dml(self):
        db = self.DB(self.dsn)
        db.execute(f"SET ROLE {self.role}")
        return db

    def test_existing_tables_need_only_dml(self):
        admin_repo = Repos(None, load_scenario(), db=self.admin)
        for name in ("anomalies", "alerts", "state", "packed"):
            getattr(admin_repo, name).ensure(force=True)                 # db init
        self.admin.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {self.role}")
        repo = Repos(None, load_scenario(), db=self.as_dml())
        repo.anomalies.bulk([("x", {"@timestamp": at(0), "series": "s"})])     # 第一次用會 ensure：只查不建
        repo.state.set("k", {"v": 1})
        self.assertEqual(repo.anomalies.get("x")["series"], "s")
        self.assertEqual(repo.state.get("k"), {"v": 1})

    def test_missing_table_without_create_fails_clearly(self):
        repo = Repos(None, load_scenario(), db=self.as_dml())
        with self.assertRaises(Exception) as cm:
            repo.state.set("k", {"v": 1})
        self.assertIn("permission denied", str(cm.exception))


class Retry(unittest.TestCase):
    """連不上時逐次拉長間隔重試（common/infra/pg.connect_with_retry）：DB 重啟、主從切換的十幾秒撐得過去；
    真的不行在 AIOPS_PG_RETRY_SECONDS 內放棄；查詢逾時不當斷線重跑。"""

    def test_gives_up_within_budget(self):
        import os
        import time
        from unittest import mock
        from common.infra.pg import DB
        with mock.patch.dict(os.environ, {"AIOPS_PG_RETRY_SECONDS": "3", "AIOPS_PG_CONNECT_TIMEOUT": "2"}):
            t0 = time.monotonic()
            with self.assertRaises(Exception):
                DB("postgresql://x@127.0.0.1:1/none").query("SELECT 1")
            took = time.monotonic() - t0
        self.assertGreater(took, 2.5)            # 等了 1、2 秒（不是 0.6 秒就放棄）
        self.assertLess(took, 6)

    def test_recovers_when_db_comes_back(self):
        import os
        import psycopg
        from unittest import mock
        from common.infra import pg
        calls, real = [], psycopg.connect

        def flaky(*a, **k):                      # 前兩次像 DB 重啟中：連線被拒絕
            calls.append(1)
            if len(calls) <= 2:
                raise psycopg.OperationalError("connection refused")
            return real(*a, **k)

        dsn = fresh_db().dsn
        with mock.patch.dict(os.environ, {"AIOPS_PG_RETRY_SECONDS": "10"}), mock.patch.object(pg.psycopg, "connect", flaky), \
             mock.patch.object(pg.time, "sleep") as slept:
            self.assertEqual(pg.DB(dsn).query("SELECT 1 AS one"), [{"one": 1}])
        self.assertEqual(len(calls), 3)
        waits = [c.args[0] for c in slept.call_args_list]
        self.assertTrue(0.8 <= waits[0] <= 1.2 and 1.6 <= waits[1] <= 2.4, waits)     # 1 秒、2 秒（±20%）

    def test_killed_session_reconnects(self):
        from common.infra.pg import DB
        admin = fresh_db()
        db = DB(admin.dsn)
        pid = db.query("SELECT pg_backend_pid() AS p")[0]["p"]
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))          # DBA 踢掉、DB 重啟：這條連線死了
        self.assertNotEqual(db.query("SELECT pg_backend_pid() AS p")[0]["p"], pid)   # 自己重連、這次的查詢照樣成功

    def test_query_timeout_is_not_retried(self):
        import os
        from unittest import mock
        from common.infra.pg import DB
        dsn = fresh_db().dsn
        with mock.patch.dict(os.environ, {"AIOPS_PG_STATEMENT_TIMEOUT": "1"}):
            db = DB(dsn)
            db.query("SELECT 1")
            with mock.patch.object(db, "_connect", side_effect=AssertionError("不該重連")):
                with self.assertRaises(Exception) as cm:
                    db.query("SELECT pg_sleep(3)")
        self.assertIn("statement timeout", str(cm.exception))

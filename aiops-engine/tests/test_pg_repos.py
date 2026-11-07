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

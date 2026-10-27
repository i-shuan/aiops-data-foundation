"""事件整合（investigator.alerts）：同一個服務合成一件、送給誰看 dept。"""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from investigator import alerts as AL

T0 = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)
GAP = timedelta(minutes=30)
_n = 0


def anom(svc, minute, *, level="dow_hour", status="anomaly", severity=None, series="http.latency_p95_s", dept="D3120",
         group="it", **extra):
    global _n
    _n += 1
    a = {"_id": f"x{_n}", "@timestamp": (T0 + timedelta(minutes=minute)).isoformat(), "series": series, "level": level,
         "keys": {"resource.attributes.service.name": svc, "attributes.http.request.method": "GET",
                  "attributes.http.route": "/lots/{id}"}, "value": 3.0, "median": 0.1, "z": 9, "dept": dept, "group": group}
    if level not in ("rule", "peer", "quality"):
        a["status"] = status
    if severity:
        a["severity"] = severity
    a.update(extra)
    return a


class Merge(unittest.TestCase):
    def test_same_service_one_alert(self):
        # 同一個服務 10 分鐘 20 筆 → 1 件
        out = AL.merge([], [anom("mes-api", i % 10, series=f"s{i % 4}") for i in range(20)], T0 + timedelta(minutes=12), GAP)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["service"], "mes-api")
        self.assertEqual(len(out[0]["anomaly_ids"]), 20)
        self.assertEqual(sorted(out[0]["series"]), ["s0", "s1", "s2", "s3"])
        self.assertEqual(out[0]["routes"], ["GET /lots/{id}"])

    def test_same_dept_different_services_are_two(self):
        # 同一個 dept、不相關的兩個服務 → 兩件（合成一件看服務，不看 dept）
        out = AL.merge([], [anom("mes-api", 0), anom("mes-report", 1)], T0 + timedelta(minutes=5), GAP)
        self.assertEqual(sorted(a["service"] for a in out), ["mes-api", "mes-report"])
        self.assertEqual({AL.owner(a) for a in out}, {("D3120", "it")})

    def test_warning_alone_does_not_open(self):
        # baseline 只跳一次（status warning）：不開
        self.assertEqual(AL.merge([], [anom("mes-api", 0, status="warning")], T0 + timedelta(minutes=1), GAP), [])

    def test_warning_attaches_to_open(self):
        out = AL.merge([], [anom("mes-api", 0), anom("mes-api", 3, status="warning", series="log.errors")],
                       T0 + timedelta(minutes=5), GAP)
        self.assertEqual(len(out), 1)
        self.assertIn("log.errors", out[0]["series"])

    def test_weak_needs_two(self):
        # 規則 warning、跟兄弟比：一筆不開，兩筆開
        one = AL.merge([], [anom("mes-api", 0, level="peer", severity="warning", series="peer.http_error_rate")],
                       T0 + timedelta(minutes=1), GAP)
        self.assertEqual(one, [])
        two = AL.merge([], [anom("mes-api", 0, level="peer", severity="warning"),
                            anom("mes-api", 2, level="rule", severity="warning", series="rule.http.latency_p95_floor")],
                       T0 + timedelta(minutes=3), GAP)
        self.assertEqual(len(two), 1)
        self.assertEqual(len(two[0]["anomaly_ids"]), 2)

    def test_critical_rule_opens_right_away(self):
        out = AL.merge([], [anom("mes-api", 0, level="rule", severity="critical", series="rule.container.restarted",
                                 message="容器重啟：ns、pod、app（1，門檻 >= 1）")], T0 + timedelta(minutes=1), GAP)
        self.assertEqual(out[0]["severity"], "critical")
        self.assertTrue(AL.headline(out[0]).startswith("[critical] mes-api：容器重啟"))

    def test_upgrade(self):
        first = AL.merge([], [anom("mes-api", 0)], T0 + timedelta(minutes=1), GAP)
        stored = [{k: v for k, v in first[0].items() if not k.startswith("_")}]
        again = AL.merge(stored, [anom("mes-api", 4, level="rule", severity="critical")], T0 + timedelta(minutes=5), GAP)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["severity"], "critical")
        self.assertTrue(again[0].get("_upgraded"))
        self.assertFalse(again[0].get("_new"))

    def test_rerun_is_idempotent(self):
        a = [anom("mes-api", 0), anom("mes-api", 2)]
        first = AL.merge([], a, T0 + timedelta(minutes=3), GAP)
        stored = [{k: v for k, v in first[0].items() if not k.startswith("_")}]
        again = AL.merge(stored, a, T0 + timedelta(minutes=4), GAP)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["anomaly_ids"], first[0]["anomaly_ids"])
        self.assertEqual(again[0]["alert_id"], first[0]["alert_id"])

    def test_resolve_after_quiet(self):
        first = AL.merge([], [anom("mes-api", 0)], T0 + timedelta(minutes=1), GAP)
        stored = [{k: v for k, v in first[0].items() if not k.startswith("_")}]
        out = AL.merge(stored, [], T0 + timedelta(minutes=31), GAP)
        self.assertEqual(out[0]["status"], "resolved")

    def test_new_alert_after_gap(self):
        # 上一件 30 分鐘沒新的、結束；之後又出事 → 新的一件（不同 id）
        first = AL.merge([], [anom("mes-api", 0)], T0 + timedelta(minutes=1), GAP)
        stored = [{k: v for k, v in first[0].items() if not k.startswith("_")}]
        out = AL.merge(stored, [anom("mes-api", 45)], T0 + timedelta(minutes=46), GAP)
        by = {a["status"]: a for a in out}
        self.assertEqual(set(by), {"open", "resolved"})
        self.assertNotEqual(by["open"]["alert_id"], by["resolved"]["alert_id"])

    def test_rerun_after_resolved_does_not_reopen(self):
        # 舊的那件已經結束：重跑時它的異常不再開新的一件，也不併進後來的那件
        old = [anom("mes-api", 0), anom("mes-api", 1)]
        first = AL.merge([], old, T0 + timedelta(minutes=40), GAP)
        self.assertEqual([a["status"] for a in first], ["resolved"])
        done = set(first[0]["anomaly_ids"])
        later = anom("mes-api", 50)
        second = AL.merge([], old + [later], T0 + timedelta(minutes=51), GAP, done)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["anomaly_ids"], [later["_id"]])

    def test_much_older_anomaly_not_attached(self):
        # 開著的那件從 50 分開始；0 分的異常（沒在已結束的件裡）也不併進來
        cur = AL.merge([], [anom("mes-api", 50)], T0 + timedelta(minutes=51), GAP)
        stored = [{k: v for k, v in cur[0].items() if not k.startswith("_")}]
        again = AL.merge(stored, [anom("mes-api", 0)], T0 + timedelta(minutes=52), GAP)
        self.assertEqual(len(again[0]["anomaly_ids"]), 1)
        self.assertEqual(AL._ts(again[0]["started_at"]), T0 + timedelta(minutes=50))

    def test_data_quality_goes_to_platform(self):
        q = anom("mes-api", 0, level="quality", series="quality.log_stopped", severity="warning")
        out = AL.merge([], [q, anom("mes-api", 0)], T0 + timedelta(minutes=1), GAP)
        cats = {a["category"]: a for a in out}
        self.assertEqual(set(cats), {"data_quality", "service"})        # 資料品質另外一件，不跟應用的併
        self.assertEqual(AL.owner(cats["data_quality"]), (None, "unassigned"))
        self.assertEqual(AL.owner(cats["service"]), ("D3120", "it"))

    def test_entity_fallbacks(self):
        self.assertEqual(AL.entity_of({"keys": {"entry.service": "wip"}}), ("service", "wip"))
        self.assertEqual(AL.entity_of({"keys": {"resource.attributes.k8s.namespace.name": "mes", "resource.attributes.k8s.pod.name": "p"}}),
                         ("namespace", "mes"))
        self.assertEqual(AL.entity_of({"keys": {"resource.attributes.k8s.node.name": "n1"}, "series": "rule.node.x"}), ("node", "n1"))


class Text(unittest.TestCase):
    def test_summary_uses_rca_conclusion(self):
        al = AL.merge([], [anom("mes-api", 0), anom("mes-api", 10, series="http.error_rate")], T0 + timedelta(minutes=11), GAP)[0]
        report = "# 根因分析\n\n## 結論（給人確認）\n\n- mes-api 變慢，主要多在 **mes-api › SELECT lots**：每個請求多花 2.8s。\n- 錯誤最早出在 **oracle**。\n"
        s = AL.summary(al, report, ZoneInfo("Asia/Taipei"))
        lines = s.splitlines()
        self.assertTrue(lines[0].startswith("[warning] mes-api："))
        self.assertIn("10-25 10:00 開始", lines[1])
        self.assertEqual(lines[2], "根因分析：mes-api 變慢，主要多在 mes-api › SELECT lots：每個請求多花 2.8s。；錯誤最早出在 oracle。")

    def test_summary_stable_while_incident_grows(self):
        # Grafana 把 summary 當 label：一件事每分鐘多併進一筆異常、持續時間變長，summary 不能變（會被當成新的一則）
        a = AL.merge([], [anom("mes-api", 0), anom("mes-api", 1)], T0 + timedelta(minutes=2), GAP)[0]
        b = AL.merge([], [anom("mes-api", 0), anom("mes-api", 1), anom("mes-api", 5), anom("mes-api", 9)], T0 + timedelta(minutes=10), GAP)[0]
        self.assertNotEqual(AL.headline(a), AL.headline(b))      # 標題照樣帶筆數
        self.assertEqual(AL.summary(a, None, timezone.utc), AL.summary(b, None, timezone.utc))
        self.assertNotIn("持續", AL.summary(b, None, timezone.utc))

    def test_summary_without_report(self):
        al = AL.merge([], [anom("mes-api", 0)], T0 + timedelta(minutes=1), GAP)[0]
        self.assertEqual(len(AL.summary(al, None, timezone.utc).splitlines()), 2)


if __name__ == "__main__":
    unittest.main()


class SummaryWithSimilar(unittest.TestCase):
    def test_similar_line(self):
        al = {"alert_id": "a-1", "category": "service", "service": "mes-api", "status": "open", "severity": "critical", "dept": "D3120", "group": "it",
              "started_at": T0.isoformat(), "updated_at": (T0 + timedelta(minutes=12)).isoformat(), "series": ["http.latency_p95_s"],
              "routes": ["GET /lots/{id}"], "anomaly_ids": ["x1"], "kinds": ["baseline"]}
        sim = [{"start": "2026-09-12T02:00:00+00:00", "root_cause_category": "db", "root_cause": "統計資訊過期", "action": "重跑 analyze",
                "why": ["同服務（mes-api）", "同一段變慢（Oracle）", "同一支 API"]}]
        s = AL.summary(al, None, ZoneInfo("Asia/Taipei"), sim)
        self.assertIn("過去相似：2026-09-12 [db] 統計資訊過期；處置：重跑 analyze（同服務（mes-api）；同一段變慢（Oracle））", s)
        self.assertNotIn("過去相似", AL.summary(al, None, ZoneInfo("Asia/Taipei"), []))

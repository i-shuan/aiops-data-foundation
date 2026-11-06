"""事件整合（investigator.alerts）：同一個服務合成一件、送給誰看 dept。"""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from investigator.alerts import merge as AL

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


class SharedDependency(unittest.TestCase):
    """開著的、不同服務的件卡在同一個外部依賴 → 互相標同源（不合併）。"""

    def al(self, svc, _id, segs, status="open"):
        return {"alert_id": _id, "category": "service", "service": svc, "status": status, "started_at": T0.isoformat(),
                "updated_at": T0.isoformat(), "anomaly_ids": [], "series": ["http.latency_p95_s"], "routes": [], "severity": "warning",
                "findings": {"segments": [{"segment": s, "delta_ms": 100.0} for s in segs], "error_origins": []}}

    def test_dependencies_from_segments(self):
        f = {"segments": [{"segment": "wip › SELECT → oracle", "delta_ms": 900}, {"segment": "wip › GET /wip", "delta_ms": 2},
                          {"segment": "mes › GET → wip", "delta_ms": -1}],
             "error_origins": [{"segment": "mes › POST → sms.example.com"}]}
        self.assertEqual(AL.dependencies(f), ["oracle", "sms.example.com"])
        self.assertEqual(AL.dependencies(None), [])

    def test_two_services_same_oracle_are_linked_not_merged(self):
        a = self.al("mes-api", "a-1", ["mes-api › SELECT LOT → oracle"])
        b = self.al("report-api", "a-2", ["report-api › SELECT REPORT → oracle"])
        c = self.al("sms-api", "a-3", ["sms-api › POST → sms.example.com"])
        changed = AL.link_shared([a, b, c])
        self.assertEqual(changed, {"a-1", "a-2"})
        self.assertEqual((a["related_alerts"], a["shared_dependency"]), (["a-2"], ["oracle"]))
        self.assertEqual((b["related_alerts"], b["shared_dependency"]), (["a-1"], ["oracle"]))
        self.assertEqual((c["related_alerts"], c["shared_dependency"]), ([], []))
        self.assertEqual(len([a, b, c]), 3)                                  # 還是三件
        self.assertIn("可能同源：a-2 也卡在 oracle", AL.summary(a, None, timezone.utc))
        self.assertNotIn("可能同源", AL.summary(c, None, timezone.utc))
        # 再跑一次沒有變：不用重寫摘要
        self.assertEqual(AL.link_shared([a, b, c]), set())

    def test_upstream_service_is_not_a_shared_dependency(self):
        # 兩個服務都呼叫 wip-worker、而 wip-worker 自己有一件：那是上下游（chain 處理），不算同源
        a = self.al("mes-api", "a-1", ["mes-api › GET → wip-worker"])
        b = self.al("report-api", "a-2", ["report-api › GET → wip-worker"])
        w = self.al("wip-worker", "a-3", ["wip-worker › SELECT → oracle"])
        AL.link_shared([a, b, w])
        self.assertEqual((a["dependencies"], a["related_alerts"]), ([], []))
        # 沒有 wip-worker 那件時，兩件都卡在 wip-worker 就算同源
        AL.link_shared([a, b])
        self.assertEqual(a["related_alerts"], ["a-2"])

    def test_resolved_and_same_service_ignored(self):
        a = self.al("mes-api", "a-1", ["mes-api › SELECT → oracle"])
        b = self.al("report-api", "a-2", ["report-api › SELECT → oracle"], status="resolved")
        AL.link_shared([a, b])
        self.assertEqual(a["related_alerts"], [])


class SummaryWithSimilar(unittest.TestCase):
    def test_similar_line(self):
        al = {"alert_id": "a-1", "category": "service", "service": "mes-api", "status": "open", "severity": "critical", "dept": "D3120", "group": "it",
              "started_at": T0.isoformat(), "updated_at": (T0 + timedelta(minutes=12)).isoformat(), "series": ["http.latency_p95_s"],
              "routes": ["GET /lots/{id}"], "anomaly_ids": ["x1"], "kinds": ["baseline"]}
        sim = [{"start": "2026-09-12T02:00:00+00:00", "root_cause_category": "db", "root_cause": "統計資訊過期", "action": "重跑 analyze",
                "score": 9.0, "why": ["同服務（mes-api）", "同一段變慢（Oracle）", "同一支 API"]}]
        s = AL.summary(al, None, ZoneInfo("Asia/Taipei"), sim)
        # case 只做分類：通知只說像哪一類、為什麼像；不給當時的根因、處置（docs/pdca/meta/2026-11-case-classify.md）
        self.assertIn("分類：像 db 類（1 件相似的 case 裡 db 佔 100%）；最像 2026-09-12（同服務（mes-api）；同一段變慢（Oracle））", s)
        self.assertNotIn("處置", s)
        self.assertNotIn("統計資訊過期", s)
        self.assertNotIn("分類", AL.summary(al, None, ZoneInfo("Asia/Taipei"), []))


def stored(alerts):
    """模擬寫進 ES 再讀回來的件（去掉 _ 開頭的計算用欄位）。"""
    return [{k: v for k, v in a.items() if not k.startswith("_")} for a in alerts]


class CrossService(unittest.TestCase):
    """跨服務（一層父子關係，docs/pdca/phase3-locate/2026-10-cross-service-aggregation.md）：
    下游出事連帶上游 → 一件、留下游；沒有呼叫關係、時間不重疊不併；多層只併一層。"""
    CALLS = {("mes-api", "wip-worker")}       # mes-api（上游）呼叫 wip-worker（下游）

    def test_upstream_joins_downstream(self):
        # 下游（wip-worker）先開、上游（mes-api）同時段異常 → 1 件、留下游；上游的異常、序列、API 都併進去
        down = anom("wip-worker", 0, level="rule", severity="critical", series="rule.db.pool_timeout",
                    message="連線池逾時：wip-worker（12，門檻 >= 5）", keys={"service": "wip-worker"})
        up = anom("mes-api", 1)
        out = AL.merge([], [down, up], T0 + timedelta(minutes=3), GAP, calls=self.CALLS)
        self.assertEqual(len(out), 1)
        al = out[0]
        self.assertEqual((al["service"], al["upstream_services"]), ("wip-worker", ["mes-api"]))
        self.assertEqual(al["anomaly_ids"], [down["_id"], up["_id"]])
        self.assertEqual(sorted(al["series"]), ["http.latency_p95_s", "rule.db.pool_timeout"])
        self.assertEqual(al["routes"], ["GET /lots/{id}"])
        self.assertEqual(AL.headline(al), "[critical] wip-worker：連線池逾時 等 2 項異常；連帶上游 mes-api")
        self.assertEqual(AL.summary(al, None, timezone.utc).splitlines()[0], "[critical] wip-worker：連線池逾時；連帶上游 mes-api")

    def test_upstream_first_is_folded_into_downstream(self):
        # 上游的異常先到、這一輪才開的件：下游開件時整件併進來，上游那件不會出現在輸出
        out = AL.merge([], [anom("mes-api", 0), anom("wip-worker", 2)], T0 + timedelta(minutes=3), GAP, calls=self.CALLS)
        self.assertEqual([a["service"] for a in out], ["wip-worker"])
        self.assertEqual(out[0]["upstream_services"], ["mes-api"])
        self.assertEqual(len(out[0]["anomaly_ids"]), 2)
        self.assertEqual(AL._ts(out[0]["started_at"]), T0)
        self.assertTrue(out[0].get("_new"))

    def test_persisted_upstream_is_merged_into_downstream(self):
        # 上一輪已寫進 ES 的上游那件：標 resolved、merged_into 指到下游那件；它的異常搬到下游那件
        first = AL.merge([], [anom("mes-api", 0)], T0 + timedelta(minutes=1), GAP, calls=self.CALLS)
        out = AL.merge(stored(first), [anom("wip-worker", 5)], T0 + timedelta(minutes=6), GAP, calls=self.CALLS)
        by = {a["service"]: a for a in out}
        self.assertEqual(set(by), {"mes-api", "wip-worker"})
        self.assertEqual((by["mes-api"]["status"], by["mes-api"]["merged_into"], by["mes-api"]["_anoms"]),
                         ("resolved", by["wip-worker"]["alert_id"], []))
        self.assertEqual(by["wip-worker"]["upstream_services"], ["mes-api"])
        self.assertEqual(len(by["wip-worker"]["anomaly_ids"]), 2)
        self.assertIn(first[0]["anomaly_ids"][0], by["wip-worker"]["anomaly_ids"])
        self.assertEqual(AL._ts(by["wip-worker"]["started_at"]), T0)

    def test_joined_upstream_keeps_attaching_across_runs(self):
        # 下游那件已在 ES、連帶過 mes-api：之後 mes-api 的異常離這件開始 40 分（> close_after）、離最後一筆 15 分 → 照同服務的規則併
        first = AL.merge([], [anom("wip-worker", 0), anom("mes-api", 1)], T0 + timedelta(minutes=2), GAP, calls=self.CALLS)
        again = AL.merge(stored(first), [anom("wip-worker", 25), anom("mes-api", 40)], T0 + timedelta(minutes=41), GAP, calls=self.CALLS)
        self.assertEqual(len(again), 1)
        self.assertEqual((len(again[0]["anomaly_ids"]), again[0]["upstream_services"]), (4, ["mes-api"]))

    def test_late_upstream_is_not_joined(self):
        # 下游那件開了 40 分鐘還在持續；上游這時才第一次異常：離下游開始超過 close_after → 不併（可能是另一件事）
        first = AL.merge([], [anom("wip-worker", 0)], T0 + timedelta(minutes=1), GAP, calls=self.CALLS)
        out = AL.merge(stored(first), [anom("wip-worker", 25), anom("mes-api", 40)], T0 + timedelta(minutes=41), GAP, calls=self.CALLS)
        by = {a["service"]: a for a in out}
        self.assertEqual(set(by), {"mes-api", "wip-worker"})
        self.assertEqual((by["wip-worker"]["upstream_services"], by["mes-api"]["upstream_services"]), ([], []))

    def test_no_call_relation_two_alerts(self):
        # 沒有呼叫關係（沒有 trace、或 trace 裡兩者沒有邊）：照舊各開一件
        for calls in (set(), {("mes-api", "mes-report")}):
            out = AL.merge([], [anom("wip-worker", 0), anom("mes-api", 1)], T0 + timedelta(minutes=2), GAP, calls=calls)
            self.assertEqual(sorted(a["service"] for a in out), ["mes-api", "wip-worker"])
            self.assertEqual([a["upstream_services"] for a in out], [[], []])

    def test_direction_decides_which_is_kept(self):
        # 方向反過來（wip-worker 呼叫 mes-api）：留的是 mes-api 那件
        out = AL.merge([], [anom("wip-worker", 0), anom("mes-api", 1)], T0 + timedelta(minutes=2), GAP, calls={("wip-worker", "mes-api")})
        self.assertEqual([(a["service"], a["upstream_services"]) for a in out], [("mes-api", ["wip-worker"])])

    def test_not_overlapping_two_alerts(self):
        # 下游 0 分開、上游 40 分才異常（close_after 30）：時間不重疊 → 下游那件結束、上游另開一件、沒有連帶
        out = AL.merge([], [anom("wip-worker", 0), anom("mes-api", 40)], T0 + timedelta(minutes=41), GAP, calls=self.CALLS)
        by = {a["service"]: a for a in out}
        self.assertEqual((by["wip-worker"]["status"], by["mes-api"]["status"]), ("resolved", "open"))
        self.assertEqual((by["wip-worker"]["upstream_services"], by["mes-api"]["upstream_services"]), ([], []))

    def test_only_one_level(self):
        # portal → mes-api → wip-worker 三個同時出事：只併直接呼叫者
        calls = {("portal", "mes-api"), ("mes-api", "wip-worker")}
        # 最下游先到：mes-api 併進 wip-worker；portal 只直接呼叫 mes-api（它沒有自己的件）→ portal 自己一件
        out = AL.merge([], [anom("wip-worker", 0), anom("mes-api", 1), anom("portal", 2)], T0 + timedelta(minutes=3), GAP, calls=calls)
        by = {a["service"]: a["upstream_services"] for a in out}
        self.assertEqual(by, {"wip-worker": ["mes-api"], "portal": []})
        # 最上游先到：portal 併進 mes-api；wip-worker 開件時 mes-api 那件已經帶著上游 → 不再被併（多層留 Act）
        out = AL.merge([], [anom("portal", 0), anom("mes-api", 1), anom("wip-worker", 2)], T0 + timedelta(minutes=3), GAP, calls=calls)
        by = {a["service"]: a["upstream_services"] for a in out}
        self.assertEqual(by, {"mes-api": ["portal"], "wip-worker": []})

    def test_rca_route_ignores_upstream_api(self):
        # 下游的異常沒有 API（規則）、上游的有：API 照舊併進 routes，但根因分析不拿上游的 API 去查下游（下游沒有那支）
        down = anom("wip-worker", 0, level="rule", severity="critical", series="rule.db.pool_timeout", keys={"service": "wip-worker"})
        al = AL.merge([], [down, anom("mes-api", 1)], T0 + timedelta(minutes=2), GAP, calls=self.CALLS)[0]
        self.assertEqual(al["routes"], ["GET /lots/{id}"])
        self.assertIsNone(AL.rca_where(al))
        own = anom("wip-worker", 0, keys={"entry.service": "wip-worker", "entry.name": "GET /wip"})
        al = AL.merge([], [own, anom("mes-api", 1)], T0 + timedelta(minutes=2), GAP, calls=self.CALLS)[0]
        self.assertEqual(sorted(al["routes"]), ["GET /lots/{id}", "GET /wip"])
        self.assertEqual(AL.rca_where(al), "GET /wip")
        plain = AL.merge([], [anom("wip-worker", 0)], T0 + timedelta(minutes=1), GAP)[0]
        self.assertEqual(AL.rca_where(plain), "GET /lots/{id}")        # 沒有連帶：照舊看 routes

    def test_owner_and_headline_prefer_downstream(self):
        # 上游的異常比較多、dept 不同：件還是送下游的 dept；標題寫下游自己最嚴重的那筆
        out = AL.merge([], [anom("wip-worker", 0, dept="D-DB", group="db"), anom("mes-api", 0, z=50), anom("mes-api", 1, z=50)],
                       T0 + timedelta(minutes=2), GAP, calls=self.CALLS)
        self.assertEqual(len(out), 1)
        self.assertEqual(AL.owner(out[0]), ("D-DB", "db"))
        self.assertEqual(AL.headline(out[0]), "[warning] wip-worker：http.latency_p95_s 3（平常 0.1） 等 3 項異常；連帶上游 mes-api")

    def test_rerun_is_idempotent(self):
        a = [anom("wip-worker", 0), anom("mes-api", 1)]
        first = AL.merge([], a, T0 + timedelta(minutes=2), GAP, calls=self.CALLS)
        again = AL.merge(stored(first), a, T0 + timedelta(minutes=3), GAP, calls=self.CALLS)
        self.assertEqual(len(again), 1)
        self.assertEqual((again[0]["alert_id"], again[0]["anomaly_ids"], again[0]["upstream_services"]),
                         (first[0]["alert_id"], first[0]["anomaly_ids"], ["mes-api"]))

    def test_data_quality_not_merged(self):
        # 資料品質那件照舊另外一件，不跟服務的併
        q = anom("mes-api", 0, level="quality", series="quality.log_stopped", severity="warning")
        out = AL.merge([], [anom("wip-worker", 0), q], T0 + timedelta(minutes=1), GAP, calls=self.CALLS)
        self.assertEqual(sorted(a["category"] for a in out), ["data_quality", "service"])

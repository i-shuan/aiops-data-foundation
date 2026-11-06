"""一件的嚴重度（investigator/severity.py）：訊號廣度（幾類、不是幾條）＋持續（滑動窗覆蓋率）＋換版上限。不連 ES。"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import aiops.service.converge.severity as S

T0 = datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc)


def ts(*minutes):
    return [T0 + timedelta(minutes=m) for m in minutes]


class Classes(unittest.TestCase):
    def test_series_to_class(self):
        for name, cls in (("http.latency_p95_s", "latency"), ("dep.latency_avg_s", "downstream"), ("dep.error_rate", "downstream"),
                          ("http.slow_ratio", "latency"),
                          ("http.error_rate", "errors"), ("rule.http.error_rate_floor", "errors"), ("apm.errors", "errors"),
                          ("rule.container.restarted", "errors"), ("pod.cpu", "saturation"), ("rule.db.pool_timeout", "saturation"),
                          ("rule.kafka.consumer_lag_growing", "saturation"), ("peer.http_error_rate", "errors"),
                          ("http.requests", "traffic"), ("dep.calls", "traffic"), ("log.lines", "traffic"), ("quality.log_stopped", "other")):
            self.assertEqual(S.signal_class(name), cls, name)


class Breadth(unittest.TestCase):
    def test_three_classes_sustained_is_critical(self):
        s = ["http.latency_p95_s", "http.error_rate", "dep.latency_avg_s"] * 10
        g = S.grade(s, ts(*range(0, 30)))
        self.assertEqual((g["severity"], g["breadth"]), ("critical", 3))
        self.assertEqual(g["classes"], ["downstream", "errors", "latency"])

    def test_three_classes_single_point_is_only_high(self):
        g = S.grade(["http.latency_p95_s", "http.error_rate", "dep.latency_avg_s"] * 2, ts(0, 0, 0, 0, 0, 0))
        self.assertEqual(g["severity"], "high")
        self.assertIn("單點", g["why"])

    def test_two_classes_is_high(self):
        self.assertEqual(S.grade(["http.latency_p95_s", "http.error_rate"] * 2, ts(0, 1, 0, 1))["severity"], "high")

    def test_same_class_many_routes_is_high_not_critical(self):
        """3 支 API 的錯誤率都跳：仍是 errors 一類 → high。"""
        g = S.grade(["http.error_rate"] * 3, ts(0, 0, 0), members=3)
        self.assertEqual((g["severity"], g["breadth"]), ("high", 1))

    def test_traffic_never_counts(self):
        self.assertEqual(S.grade(["http.requests", "dep.calls"] * 15, ts(*range(0, 30)))["severity"], "warning")
        g = S.grade(["http.requests", "http.error_rate", "http.latency_p95_s"] * 5, ts(*range(0, 5)))
        self.assertEqual((g["severity"], g["breadth"]), ("high", 2))

    def test_one_stray_point_does_not_make_a_class(self):
        """46 筆錯誤率 ＋ 1 筆延遲：延遲那一筆不算一個維度 → 單一類、持續 → high（不是 critical）。"""
        g = S.grade(["http.error_rate"] * 46 + ["http.latency_p95_s"], ts(*range(0, 47)))
        self.assertEqual((g["severity"], g["breadth"], g["classes"]), ("high", 1, ["errors"]))

    def test_single_point_single_class_is_warning(self):
        g = S.grade(["http.error_rate", "http.error_rate"], ts(0, 0))
        self.assertEqual(g["severity"], "warning")
        self.assertIn("沒持續", g["why"])
        self.assertEqual(S.grade(["http.error_rate"], ts(0))["severity"], "warning")   # 一筆：連一類都算不上

    def test_rule_critical_wins(self):
        self.assertEqual(S.grade(["rule.container.restarted"], ts(0), rule_critical=True)["severity"], "critical")


class Duration(unittest.TestCase):
    def test_sustained_single_class_is_high(self):
        g = S.grade(["http.error_rate"] * 12, ts(*range(0, 12)))
        self.assertEqual((g["severity"], g["sustained"], g["span"]), ("high", True, 11))

    def test_gaps_inside_window_do_not_reset(self):
        """第 3、7 分鐘掉回去：15 分鐘窗內 13/15 有異常（87%）→ 還是持續。"""
        when = ts(*[m for m in range(0, 15) if m not in (3, 7)])
        g = S.grade(["http.error_rate"] * len(when), when)
        self.assertEqual((g["severity"], g["sustained"]), ("high", True))
        self.assertAlmostEqual(g["coverage"], 13 / 15, places=2)

    def test_sparse_points_over_long_span_are_not_sustained(self):
        g = S.grade(["http.error_rate"] * 4, ts(0, 10, 20, 30))
        self.assertEqual((g["severity"], g["sustained"]), ("warning", False))

    def test_too_short_is_not_sustained(self):
        self.assertFalse(S.grade(["http.error_rate"] * 8, ts(*range(0, 8)))["sustained"])


class Deploy(unittest.TestCase):
    SERIES = ["http.latency_p95_s", "http.error_rate", "dep.latency_avg_s"] * 2

    def test_capped_within_grace_then_reevaluated(self):
        g = S.grade(self.SERIES, ts(*range(0, 6)), now=T0 + timedelta(minutes=6), deploy_at=T0 - timedelta(minutes=1))
        self.assertEqual((g["severity"], g["capped"]), ("warning", True))
        self.assertIn("原本 critical", g["why"])
        # 最後一筆異常離換版 20 分鐘：過了上限還沒收斂 → 照常升級；就算現在才回放也一樣（看異常的時間，不看現在）
        g2 = S.grade(self.SERIES, ts(*range(0, 20)), now=T0 + timedelta(hours=5), deploy_at=T0 - timedelta(minutes=1))
        self.assertEqual((g2["severity"], g2["capped"]), ("critical", False))

    def test_transient_after_deploy_stays_warning_even_when_replayed_later(self):
        g = S.grade(self.SERIES, ts(*range(0, 4)), now=T0 + timedelta(hours=2), deploy_at=T0)
        self.assertEqual((g["severity"], g["capped"]), ("warning", True))

    def test_deploy_long_ago_is_ignored(self):
        g = S.grade(["http.error_rate", "http.latency_p95_s"] * 2, ts(0, 1, 0, 1), now=T0 + timedelta(minutes=2), deploy_at=T0 - timedelta(hours=3))
        self.assertEqual(g["severity"], "high")


class GradeAlert(unittest.TestCase):
    """cmd_alert.grade_alert：舊異常用 _mget 補齊、只算這件自己服務的、換版查不到不擋。"""

    def test_reads_old_anomalies_and_detects_deploy(self):
        import aiops.service.converge.alert_service as C
        from aiops.core.repository import Repos
        from aiops.scenario import load_scenario
        SC = load_scenario()
        old = [{"_id": f"o{i}", "_source": {"@timestamp": (T0 + timedelta(minutes=i)).isoformat(), "series": "http.error_rate", "keys": {"service": "pay-api", "route": "POST /pay"}}} for i in range(5)]

        class FakeES:
            def req(self, method, path, body=None, ok=None):
                self.ids = body["ids"]
                return {"docs": [{**o, "found": True} for o in old]}

            def search(self, index, body):
                return {"aggregations": {"v": {"buckets": [
                    {"key": "1.4.0", "first": {"value": (T0 - timedelta(days=5)).timestamp() * 1000}},
                    {"key": "1.5.0", "first": {"value": (T0 - timedelta(minutes=2)).timestamp() * 1000}}]}}}
        es = FakeES()
        new = [{"_id": f"n{i}", "@timestamp": (T0 + timedelta(minutes=i)).isoformat(), "series": "http.latency_p95_s", "keys": {"service": "pay-api"}} for i in (5, 6)]
        al = C.Alert(alert_id="a", category="service", service="pay-api", status="open", severity="warning", started_at=T0, updated_at=T0 + timedelta(minutes=6),
                     anomaly_ids=[o["_id"] for o in old] + [n["_id"] for n in new], series=["http.error_rate", "http.latency_p95_s"], routes=[], new=False,
                     anoms=new)
        cfg = {"indices": {"anomalies": "aiops-anomalies"}, "alerts": {}}
        C.grade_alert(Repos(es, SC), cfg, al, T0 + timedelta(minutes=7))
        self.assertEqual(es.ids, [o["_id"] for o in old])                       # 只補這一輪沒有的
        self.assertEqual((al.severity, al.deploy_at), ("warning", T0 - timedelta(minutes=2)))   # 2 類 → 本該 high；異常都在換版後 8 分鐘內 → warning
        self.assertIn("原本 high", al.severity_why)
        # 異常繼續到換版後 20 分鐘：沒收斂 → high
        al.anoms = new + [{"_id": "n9", "@timestamp": (T0 + timedelta(minutes=18)).isoformat(), "series": "http.latency_p95_s", "keys": {"service": "pay-api"}}]
        al.anomaly_ids.append("n9")
        C.grade_alert(Repos(es, SC), cfg, al, T0 + timedelta(minutes=19))
        self.assertEqual(al.severity, "high")

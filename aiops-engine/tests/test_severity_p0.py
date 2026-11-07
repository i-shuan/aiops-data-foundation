"""P0 服務升嚴重度（docs/pdca/meta/2026-11-cases-tier-drop.md）。為了確認：
1. 名字或 regex 對得上才算 P0；寫壞的 regex 不讓程式掛掉。
2. P0 的件升一級（warning→high、high→critical、critical 不變），why 寫出來；不是 P0 不動。
3. 換版後的上限 warning，P0 也升成 high（換版壞了要叫人）。
4. 告警流程真的用到 rca.yaml 的 alerts.p0_services。"""
import unittest
from datetime import datetime, timedelta, timezone

import incident.alert_severity as SEV
from incident.config.rca import Alerts


T0 = datetime(2026, 11, 7, 2, 0, tzinfo=timezone.utc)


class P0(unittest.TestCase):
    def test_is_p0(self):
        pats = ["order-api", "payment-.*", "bad[regex"]
        self.assertTrue(SEV.is_p0("order-api", pats))
        self.assertTrue(SEV.is_p0("payment-gw", pats))
        self.assertTrue(SEV.is_p0("bad[regex", pats))
        self.assertFalse(SEV.is_p0("order-api-v2", pats))           # 要整個名字對得上
        self.assertFalse(SEV.is_p0(None, pats))
        self.assertFalse(SEV.is_p0("x", []))

    def test_escalate_one_level(self):
        for before, after in (("warning", "high"), ("high", "critical"), ("critical", "critical")):
            g = SEV.escalate({"severity": before, "why": "w"}, True)
            self.assertEqual(g["severity"], after)
            self.assertIn("P0", g["why"])
        self.assertEqual(SEV.escalate({"severity": "warning", "why": "w"}, False), {"severity": "warning", "why": "w"})

    def test_single_point_and_deploy_cap(self):
        g = SEV.grade(["http.latency"], [T0])
        self.assertEqual(g["severity"], "warning")
        self.assertEqual(SEV.escalate(g, True)["severity"], "high")
        ts = [T0 + timedelta(minutes=i) for i in range(12)]
        g = SEV.grade(["http.latency"] * 12 + ["http.errors"] * 12, ts + ts, deploy_at=T0 + timedelta(minutes=5))
        self.assertTrue(g["capped"])
        self.assertEqual(SEV.escalate(g, True)["severity"], "high")

    def test_config_field(self):
        self.assertEqual(Alerts().p0_services, [])
        self.assertEqual(Alerts(p0_services=["order-api"]).p0_services, ["order-api"])


if __name__ == "__main__":
    unittest.main()

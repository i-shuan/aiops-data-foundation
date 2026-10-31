"""相似事故檢索（investigator.similar）：指紋、為什麼像、檢索、留一法評測。假的 ES，不用連線。"""
import unittest

from investigator import similar as SIM


class FakeES:
    """只做 similar 用到的：status 的 terms 過濾、exists、must_not ids；should 不算（分數由 explain 自己算）。"""
    def __init__(self, incidents: dict):
        self.docs = incidents

    def search(self, index, body):
        q = body["query"]["bool"]
        out = []
        for _id, s in self.docs.items():
            ok = True
            for f in q.get("filter", []):
                if "terms" in f:
                    (k, vals), = f["terms"].items()
                    ok &= s.get(k) in vals
                if "exists" in f:
                    ok &= s.get(f["exists"]["field"]) is not None
            for m in q.get("must_not", []):
                ok &= _id not in m["ids"]["values"]
            if ok:
                out.append({"_id": _id, "_source": s})
        return {"hits": {"hits": out}}


def findings(seg="mes-api › wip-worker › Oracle SELECT lots 123", err=None, pats=()):
    f = {"segments": [{"segment": seg, "delta_ms": 900.0, "now_ms": 1000, "ref_ms": 100}], "error_origins": [], "new_patterns": []}
    if err:
        f["error_origins"].append({"segment": err, "now_errors": 40, "ref_errors": 0})
    f["new_patterns"] = [{"pattern": p, "is_new": True} for p in pats]
    return f


ALERT = {"alert_id": "a-1", "service": "mes-api", "routes": ["GET /lots/{id}"], "series": ["http.latency_p95_s"]}


def incident(cat, fp, status="confirmed", **kw):
    return {"status": status, "root_cause_category": cat, "fingerprint": fp, "title": f"{cat} 事故", "start": "2026-09-12T02:00:00+00:00",
            "services": fp.get("services"), "root_cause": f"{cat} 的根因", "action": "重跑 analyze", "how_detected": "看 Oracle 段", **kw}


class Fingerprint(unittest.TestCase):
    def test_fields_and_number_stripping(self):
        fp = SIM.fingerprint(ALERT, findings(err="wip-worker › Kafka produce"))
        self.assertEqual(fp["services"], ["mes-api"])
        self.assertEqual(fp["routes"], ["GET /lots/{id}"])
        # SQL 裡的數字去掉，同一種呼叫才對得起來
        self.assertEqual(fp["slow_segments"], ["mes-api › wip-worker › Oracle SELECT lots #"])
        self.assertEqual(fp["error_origins"], ["wip-worker › Kafka produce"])

    def test_no_findings(self):
        fp = SIM.fingerprint(ALERT, None)
        self.assertEqual((fp["slow_segments"], fp["error_origins"], fp["patterns"]), ([], [], []))
        self.assertEqual(fp["series"], ["http.latency_p95_s"])


class Explain(unittest.TestCase):
    def test_why_lists_only_shared_fields(self):
        mine = SIM.fingerprint(ALERT, findings())
        other = {"fingerprint": {**mine, "routes": ["POST /other"]}}
        score, why = SIM.explain(mine, other)
        self.assertIn("同一段變慢（mes-api › wip-worker › Oracle SELECT lots #）", why)
        self.assertIn("同服務（mes-api）", why)
        self.assertFalse(any("API" in w for w in why))   # 路由不同就不能說同一支 API
        self.assertEqual(score, SIM.WEIGHTS["slow_segments"] + SIM.WEIGHTS["services"] + SIM.WEIGHTS["series"])


class Search(unittest.TestCase):
    def setUp(self):
        self.fp = SIM.fingerprint(ALERT, findings())
        self.es = FakeES({
            "i-db": incident("db", self.fp),                                       # 一模一樣
            "i-net": incident("network", {"services": ["mes-api"], "routes": [], "series": ["log.errors"], "slow_segments": [], "error_origins": [], "patterns": []}),  # 只有同服務：3 分，不夠
            "i-auto": incident("db", self.fp, status="auto"),                      # 還沒人確認：不拿來教人
            "i-other": incident("db", {"services": ["pay"], "routes": [], "series": [], "slow_segments": ["pay › Oracle SELECT lots #"], "error_origins": [], "patterns": []}),
        })

    def test_only_confirmed_and_above_min_score(self):
        got = SIM.search(self.es, self.fp)
        self.assertEqual([g["incident_id"] for g in got], ["i-db"])
        self.assertEqual(got[0]["root_cause"], "db 的根因")
        self.assertTrue(got[0]["why"])

    def test_exclude_self(self):
        self.assertEqual(SIM.search(self.es, self.fp, exclude_id="i-db"), [])

    def test_empty_fingerprint_returns_nothing(self):
        self.assertEqual(SIM.search(self.es, {"services": [], "routes": []}), [])

    def test_evaluate_leave_one_out(self):
        # 加一件跟 i-db 同根因、同指紋的：i-db 查得到它、它查得到 i-db → 兩件都命中；i-net、i-other 沒有同類的 → 沒命中
        self.es.docs["i-db2"] = incident("db", self.fp)
        r = SIM.evaluate(self.es)
        self.assertEqual(r["n"], 4)
        self.assertEqual(r["hit"], 2)
        self.assertAlmostEqual(r["recall"], 0.5)

    def test_format(self):
        lines = SIM.format_similar(SIM.search(self.es, self.fp))
        text = "\n".join(lines)
        self.assertIn("過去相似的事故", text)
        self.assertIn("當時的根因：[db] db 的根因", text)
        self.assertIn("怎麼看出來的：看 Oracle 段", text)
        self.assertEqual(SIM.format_similar([]), [])


if __name__ == "__main__":
    unittest.main()

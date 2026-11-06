"""相似事故檢索（investigator.similar）：指紋、為什麼像、檢索、留一法評測。假的 ES，不用連線。"""
import unittest

import service.rca.similar as SIM
from core.repository import Repos
from scenario import load_scenario

SC = load_scenario()


class FakeES:
    """只做 similar 用到的：status 的 terms 過濾、exists、must_not ids、size（照 docs 的順序切前 size 件，當作 ES 的排名）；
    should 不算（分數由 explain 自己算）。"""
    def __init__(self, incidents: dict):
        self.docs = incidents
        self.bodies = []

    def search(self, index, body):
        self.bodies.append(body)
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
        return {"hits": {"hits": out[:body.get("size", 10)]}}


def findings(seg="mes-api › wip-worker › Oracle SELECT lots 123", err=None, pats=()):
    f = {"segments": [{"segment": seg, "delta_ms": 900.0, "now_ms": 1000, "ref_ms": 100}], "error_origins": [], "new_patterns": []}
    if err:
        f["error_origins"].append({"segment": err, "now_errors": 40, "ref_errors": 0})
    f["new_patterns"] = [{"pattern": p, "is_new": True} for p in pats]
    return f


ALERT = {"alert_id": "a-1", "service": "mes-api", "routes": ["GET /lots/{id}"], "series": ["http.latency_p95_s"]}


def incident(cat, fp, status="confirmed", **kw):
    fp = SIM.Fingerprint.of(fp).to_doc()   # 存進 ES 的是 dict
    return {"status": status, "root_cause_category": cat, "fingerprint": fp, "title": f"{cat} 事故", "start": "2026-09-12T02:00:00+00:00",
            "services": fp.get("services"), "root_cause": f"{cat} 的根因", "action": "重跑 analyze", "how_detected": "看 Oracle 段", **kw}


class Fingerprint(unittest.TestCase):
    def test_fields_and_number_stripping(self):
        fp = SIM.fingerprint(ALERT, findings(err="wip-worker › Kafka produce"))
        self.assertEqual(fp.services, ["mes-api"])
        self.assertEqual(fp.routes, ["GET /lots/{id}"])
        # SQL 裡的數字去掉，同一種呼叫才對得起來
        self.assertEqual(fp.slow_segments, ["mes-api › wip-worker › Oracle SELECT lots #"])
        self.assertEqual(fp.error_origins, ["wip-worker › Kafka produce"])

    def test_no_findings(self):
        fp = SIM.fingerprint(ALERT, None)
        self.assertEqual((fp.slow_segments, fp.error_origins, fp.patterns), ([], [], []))
        self.assertEqual(fp.series, ["http.latency_p95_s"])


class Explain(unittest.TestCase):
    def test_why_lists_only_shared_fields(self):
        mine = SIM.fingerprint(ALERT, findings())
        other = {"fingerprint": {**mine.to_doc(), "routes": ["POST /other"]}}
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
        got = SIM.search(Repos(self.es), self.fp)
        self.assertEqual([g["incident_id"] for g in got], ["i-db"])
        self.assertEqual(got[0]["root_cause"], "db 的根因")
        self.assertTrue(got[0]["why"])

    def test_exclude_self(self):
        self.assertEqual(SIM.search(Repos(self.es), self.fp, exclude_id="i-db"), [])

    def test_empty_fingerprint_returns_nothing(self):
        self.assertEqual(SIM.search(Repos(self.es), {"services": [], "routes": []}), [])

    def test_evaluate_leave_one_out(self):
        # 加一件跟 i-db 同根因、同指紋的：i-db 查得到它、它查得到 i-db → 兩件都命中；i-net、i-other 沒有同類的 → 沒命中
        self.es.docs["i-db2"] = incident("db", self.fp)
        r = SIM.evaluate(Repos(self.es))
        self.assertEqual(r["n"], 4)
        self.assertEqual(r["hit"], 2)
        self.assertAlmostEqual(r["recall"], 0.5)

    def test_best_match_beyond_es_top20_is_found(self):
        """docs/pdca/phase5-learning-loop/2026-10-fix-critical.md Act「相似事故 ES 前 20 截斷」：ES 排前面的 25 件只有同服務（3 分、不夠像），
        一模一樣的那件排第 26；候選只要 20 件就被截掉，要 200 件才找得到。"""
        only_svc = {"services": ["mes-api"], "routes": [], "series": [], "slow_segments": [], "error_origins": [], "patterns": []}
        docs = {f"i-weak{i}": incident("network", only_svc) for i in range(25)}
        docs["i-best"] = incident("db", self.fp)
        es = FakeES(docs)
        self.assertEqual([g["incident_id"] for g in SIM.search(Repos(es, SC), self.fp)], ["i-best"])
        self.assertEqual(es.bodies[-1]["size"], SIM.CANDIDATES)
        self.assertGreaterEqual(SIM.CANDIDATES, 26)
        # 以前的 20 件就會漏掉；size 可以指定
        self.assertEqual(SIM.search(Repos(es, SC), self.fp, size=20), [])
        self.assertEqual(es.bodies[-1]["size"], 20)

    def test_format(self):
        lines = SIM.format_similar(SIM.search(Repos(self.es), self.fp))
        text = "\n".join(lines)
        # case 只做分類：段落標題是分類，清單只有日期、分類、標題、為什麼像；不列當時的根因、處置
        self.assertTrue(lines[0].startswith("## 分類（依過去相似的 case）：像 db 類"), lines[0])
        self.assertIn("[db] db 事故（為什麼像：", text)
        self.assertNotIn("當時的根因", text)
        self.assertNotIn("處置", text)
        self.assertNotIn("看 Oracle 段", text)
        self.assertEqual(SIM.format_similar([]), [])


if __name__ == "__main__":
    unittest.main()

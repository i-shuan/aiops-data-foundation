"""相似事故檢索（llm/similar.py）：指紋、為什麼像、檢索、留一法評測。事故存在測試用的 PostgreSQL。"""
import unittest

import llm.similar as SIM
from common.infra.repos import Repos
from common.scenario import load_scenario
from tests.pg import fresh_db

SC = load_scenario()


def store(incidents: dict, db=None) -> Repos:
    """事故寫進測試用的 PostgreSQL（tests/conftest.py 指好的，每個測試結束清空；db 給了就寫那個）。"""
    repo = Repos(None, SC, db=db)
    repo.incidents.bulk(list(incidents.items()))
    return repo


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
        self.repo = store({
            "i-db": incident("db", self.fp),                                       # 一模一樣
            "i-net": incident("network", {"services": ["mes-api"], "routes": [], "series": ["log.errors"], "slow_segments": [], "error_origins": [], "patterns": []}),  # 只有同服務：3 分，不夠
            "i-auto": incident("db", self.fp, status="auto"),                      # 還沒人確認：不拿來教人
            "i-other": incident("db", {"services": ["pay"], "routes": [], "series": [], "slow_segments": ["pay › Oracle SELECT lots #"], "error_origins": [], "patterns": []}),
        })

    def test_only_confirmed_and_above_min_score(self):
        got = SIM.search(self.repo, self.fp)
        self.assertEqual([g["incident_id"] for g in got], ["i-db"])
        self.assertEqual(got[0]["root_cause"], "db 的根因")
        self.assertTrue(got[0]["why"])

    def test_exclude_self(self):
        self.assertEqual(SIM.search(self.repo, self.fp, exclude_id="i-db"), [])

    def test_empty_fingerprint_returns_nothing(self):
        self.assertEqual(SIM.search(self.repo, {"services": [], "routes": []}), [])

    def test_evaluate_leave_one_out(self):
        # 加一件跟 i-db 同根因、同指紋的：i-db 查得到它、它查得到 i-db → 兩件都命中；i-net、i-other 沒有同類的 → 沒命中
        self.repo.incidents.bulk([("i-db2", incident("db", self.fp))])
        r = SIM.evaluate(self.repo)
        self.assertEqual(r["n"], 4)
        self.assertEqual(r["hit"], 2)
        self.assertAlmostEqual(r["recall"], 0.5)

    def test_best_match_is_not_cut_by_the_candidate_limit(self):
        """docs/pdca/phase5-learning-loop/2026-10-fix-critical.md Act「相似事故前 20 截斷」：25 件只有同服務（3 分、不夠像）、而且比較新，
        一模一樣的那件最舊。候選照權重排（同一段變慢 4 分在前），只要 20 件也找得到它。"""
        only_svc = {"services": ["mes-api"], "routes": [], "series": [], "slow_segments": [], "error_origins": [], "patterns": []}
        docs = {f"i-weak{i}": incident("network", only_svc, start=f"2026-10-{i + 1:02d}T00:00:00+00:00") for i in range(25)}
        docs["i-best"] = incident("db", self.fp, start="2026-01-01T00:00:00+00:00")
        repo = store(docs, fresh_db())          # 不跟 setUp 放的那幾件混在一起
        self.assertEqual([g["incident_id"] for g in SIM.search(repo, self.fp)], ["i-best"])
        self.assertEqual([g["incident_id"] for g in SIM.search(repo, self.fp, size=20)], ["i-best"])
        self.assertEqual([i for i, _ in repo.incidents.similar(self.fp, SIM.CONFIRMED, size=1)], ["i-best"])

    def test_format(self):
        lines = SIM.format_similar(SIM.search(self.repo, self.fp))
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

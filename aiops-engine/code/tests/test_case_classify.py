"""case 只做告警分類（docs/pdca/meta/2026-11-case-classify.md）：分類與信心、留一法準確率、學權重、表單預帶、DB 匯入、precision 建議。不連 ES。"""
import os
import sqlite3
import tempfile
import unittest

import pandas as pd

from detector import feedback as F
from feedback_web import app as A
from investigator import importer as IMP
from investigator import similar as SIM


def fp(svc="mes-api", seg="mes-api › SELECT WIP → oracle", series=("http.error_rate",), route="GET /lots"):
    return {"services": [svc], "routes": [route], "series": list(series), "slow_segments": [seg], "error_origins": [], "patterns": []}


def case(cat, f, _id=None):
    return (_id or f"{cat}-{abs(hash(str(f))) % 10_000}", {"status": "confirmed", "root_cause_category": cat, "fingerprint": f, "title": f"{cat} 事故",
                                                             "start": "2026-09-12T02:00:00+00:00"})


class Classify(unittest.TestCase):
    def test_vote_and_confidence(self):
        ranked = [{"incident_id": "a", "score": 9.0, "root_cause_category": "db"}, {"incident_id": "b", "score": 7.0, "root_cause_category": "db"},
                  {"incident_id": "c", "score": 4.0, "root_cause_category": "network"}]
        cls = SIM.classify(ranked)
        self.assertEqual(cls["category"], "db")
        self.assertAlmostEqual(cls["confidence"], 0.8)                        # 16 / 20
        self.assertEqual((cls["n"], cls["based_on"]), (3, ["a", "b", "c"]))
        self.assertEqual(list(cls["votes"]), ["db", "network"])
        self.assertIn("像 db 類（3 件相似的 case 裡 db 佔 80%）", SIM.describe_class(cls))

    def test_no_candidates_or_no_category_means_no_guess(self):
        self.assertIsNone(SIM.classify([]))
        self.assertIsNone(SIM.classify([{"incident_id": "a", "score": 9.0}]))
        self.assertEqual(SIM.describe_class(None), "沒有相似的過去 case，不分類")

    def test_weights_change_ranking(self):
        f = fp()
        cands = [case("db", {**fp(), "routes": []}, "db1"), case("network", {**fp(seg="x"), "slow_segments": []}, "net1")]
        self.assertEqual(SIM.classify(SIM.rank(f, cands))["category"], "db")                    # 預設：同一段變慢 4 分贏
        w = {**SIM.WEIGHTS, "slow_segments": 0.0, "routes": 6.0}
        self.assertEqual(SIM.classify(SIM.rank(f, cands, weights=w))["category"], "network")    # 路由權重大：net1 多一支 API


class Evaluate(unittest.TestCase):
    def cases(self, n_db=6, n_net=6):
        out = []
        for i in range(n_db):
            out.append(case("db", fp(seg="mes-api › SELECT WIP → oracle", route=f"GET /lots/{i % 2}"), f"db{i}"))
        for i in range(n_net):
            out.append(case("network", fp(svc="erp-gw", seg="erp-gw › POST erp → external", series=("http.latency_p95_s",), route=f"POST /erp/{i % 2}"), f"net{i}"))
        return out

    def test_leave_one_out_accuracy(self):
        r = SIM.evaluate_cases(self.cases())
        self.assertEqual((r["n"], r["correct"], r["accuracy"]), (12, 12, 1.0))
        self.assertEqual(r["rows"][0]["predicted"], "db")

    def test_learn_weights_refuses_small_sets(self):
        got = SIM.learn_weights(self.cases())
        self.assertFalse(got["ok"])
        self.assertIn("少於 50", got["reason"])

    def test_learn_weights_improves_or_keeps(self):
        # 兩類只差在 series：手設的 series 權重 1、同服務 3 → 全部投給同服務的那類（全錯）；學權重會把 series 拉高
        cases = [case("db", fp(seg="s", series=("a",), route="r"), f"db{i}") for i in range(30)] + \
                [case("network", fp(seg="s", series=("b",), route="r"), f"net{i}") for i in range(30)]
        got = SIM.learn_weights(cases, rounds=2)
        self.assertTrue(got["ok"])
        self.assertGreaterEqual(got["after"], got["before"])
        self.assertEqual(SIM.evaluate_cases(cases, weights=got["weights"])["accuracy"], got["after"])


class FormPrefill(unittest.TestCase):
    ALERT = {"alert_id": "a-1", "title": "t", "service": "mes-api", "dept": "D", "severity": "critical", "status": "open", "started_at": "2026-11-02T12:00:00Z",
             "similar": [{"start": "2026-09-12T02:00:00+00:00", "root_cause_category": "db", "root_cause": "統計資訊過期", "action": "重跑 analyze",
                          "title": "lots 變慢", "score": 9.0, "why": ["同服務（mes-api）"]}]}

    def test_suggested_category_prefilled_and_no_action_shown(self):
        d = A.form_data({**self.ALERT, "suggested_category": "db", "category_confidence": 1.0})
        self.assertEqual(d["suggested_category"], "db")
        j = A.form_json(d)
        self.assertEqual(j["root_cause_category"], "db")                       # Grafana 表單那格的 id
        self.assertIn("[db] lots 變慢｜為什麼像：同服務（mes-api）", j["similar"])
        self.assertNotIn("重跑 analyze", j["similar"])
        html = A.render_form(d)
        self.assertIn('<option value="db" selected', html)
        self.assertIn("預帶 db", html)
        sim_section = html.split("過去相似的事故")[1].split("</ul>")[0]
        self.assertIn("[db] lots 變慢", sim_section)
        self.assertNotIn("重跑 analyze", sim_section)           # 「處置」欄位的範例字還在表單裡，但 case 的處置不給

    def test_old_alert_without_field_is_classified_on_the_fly(self):
        d = A.form_data(self.ALERT)
        self.assertEqual(d["suggested_category"], "db")
        self.assertEqual(A.form_json(A.form_data({**self.ALERT, "similar": []}))["root_cause_category"], "")


class DbImport(unittest.TestCase):
    def test_sqlite_rows_go_through_normalize(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cases.db")
            con = sqlite3.connect(path)
            con.execute("create table incidents (subject text, opened text, closed text, svc text, cat text, cause text, act text, how text, chk text)")
            con.executemany("insert into incidents values (?,?,?,?,?,?,?,?,?)", [
                ("lots 變慢", "2026-03-12 02:10", "2026-03-12 03:00", "mes-api|wip-worker", "db", "統計過期", "重跑 stats", "AWR", "AWR|trace"),
                ("缺欄位", "2026-03-13 02:10", "2026-03-13 03:00", "mes-api", "", "x", "y", "z", "a")])
            con.commit(); con.close()
            rows = IMP.DbRows(f"sqlite:///{path}", "select subject as title, opened as start, closed as end, svc as services, cat as root_cause_category, "
                                                    "cause as root_cause, act as action, how as how_detected, chk as checked from incidents")
            docs, skipped = IMP.parse(rows)
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0][1]["services"], ["mes-api", "wip-worker"])
            self.assertEqual(docs[0][1]["fingerprint"]["services"], ["mes-api", "wip-worker"])
            self.assertEqual(skipped, [(2, "缺 root_cause_category")])

    def test_password_from_env_and_masked(self):
        os.environ["CASE_PW"] = "s3cret"
        r = IMP.DbRows("postgresql://u:${CASE_PW}@h/db", "select 1")
        self.assertIn("s3cret", r.url)
        self.assertNotIn("s3cret", str(r))


class Precision(unittest.TestCase):
    def rows(self, **kw):
        base = {"series": "http.error_rate", "alerts": 20, "reviewed": 12, "true_positive": 6, "false_positive": 6, "wrong_root_cause": 0, "missed": 0,
                "fp_rate": 0.5, "reviewed_rate": 0.6}
        return pd.DataFrame([{**base, **kw}])

    def test_low_precision_loosens_whole_series(self):
        got = F.suggest_by_precision(self.rows(), 5.0)
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["series"], got[0]["match"], got[0]["z"], got[0]["status"]), ("http\\.error_rate", {}, 7.0, "proposed"))
        self.assertIn("precision 50%", got[0]["reason"])

    def test_high_precision_with_missed_tightens(self):
        got = F.suggest_by_precision(self.rows(true_positive=11, wrong_root_cause=1, false_positive=0, missed=1), 5.0)
        self.assertEqual(got[0]["z"], 4.0)
        self.assertEqual(F.suggest_by_precision(self.rows(true_positive=12, false_positive=0, missed=0), 5.0), [])   # 沒漏報不收緊

    def test_too_few_reviewed_or_in_between_is_silent(self):
        self.assertEqual(F.suggest_by_precision(self.rows(reviewed=9, true_positive=3, false_positive=6), 5.0), [])
        self.assertEqual(F.suggest_by_precision(self.rows(true_positive=10, false_positive=2), 5.0), [])              # 83%

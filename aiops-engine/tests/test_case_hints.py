"""歷史 case 補根因分析（docs/pdca/meta/2026-11-cases-tier-drop.md）。為了確認：
1. 線索只列「這次資料已經看到變化」的段（變慢的 delta > 0、錯變多的）；過去常出事但這次沒變化的不列。
2. 過去出現過的件數多的排前面；一樣多照這次變化的順序；段名去數字後才比（SQL 裡的 id）。
3. 先查清單：同一類、已確認的事故裡老師傅勾過的排前面，不夠用情境資料檔的補；沒分類就沒有。
4. 告警流程把線索與清單接在分類後面、存進 findings；讀不到事故不擋。
5. 考卷：答案的段在這次前三段就算對；沒 trace 略過不算錯；沒答案不考；跟上次比列出變差的、回 1。"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import incident.rca_eval as EVAL
import llm.similar as SIM
from common.scenario import load_scenario

DB = "mes-api › SELECT WIP WHERE id=123 → oracle"


def case(_id, slow=(), err=(), cat="db", checked=(), start="2026-10-01T02:00:00+00:00", title=None, services=("mes-api",)):
    return (_id, {"status": "confirmed", "title": title or _id, "start": start, "root_cause_category": cat, "checked": list(checked),
                  "services": list(services), "fingerprint": {"services": list(services), "slow_segments": list(slow), "error_origins": list(err)}})


FINDINGS = {"segments": [{"segment": "mes-api › GET /lots", "delta_ms": 300, "ref_ms": 20, "now_ms": 320},
                         {"segment": DB, "delta_ms": 120, "ref_ms": 10, "now_ms": 130},
                         {"segment": "mes-api › redis", "delta_ms": -5, "ref_ms": 3, "now_ms": 1}],
            "error_origins": [{"segment": "wip-worker › kafka", "now_errors": 9, "ref_errors": 0}],
            "requests": {"requests": 50}}


class Hints(unittest.TestCase):
    def test_only_segments_with_evidence_now(self):
        cases = [case("a", slow=["mes-api › redis"]), case("b", slow=["mes-api › SELECT WIP WHERE id=# → oracle"])]
        hints = SIM.segment_hints(FINDINGS, cases)
        self.assertEqual([h["segment"] for h in hints], [DB])          # redis 這次沒變慢，過去常出事也不列
        self.assertEqual(hints[0]["kind"], "slow")
        self.assertIn("120", hints[0]["evidence"])

    def test_more_past_cases_first_then_current_order(self):
        cases = [case("a", slow=["mes-api › GET /lots"]), case("b", err=["wip-worker › kafka"]), case("c", err=["wip-worker › kafka"], start="2026-10-05")]
        hints = SIM.segment_hints(FINDINGS, cases)
        self.assertEqual([h["segment"] for h in hints], ["wip-worker › kafka", "mes-api › GET /lots"])
        self.assertEqual([c["id"] for c in hints[0]["cases"]], ["c", "b"])        # 新的在前
        self.assertEqual(SIM.segment_hints({}, cases), [])

    def test_checklist_past_first_then_defaults(self):
        cases = [case("a", checked=["DB 連線池", "Kibana log"]), case("b", checked=["DB 連線池"]), case("c", cat="network", checked=["網路"])]
        todo = SIM.checklist("db", cases, {"db": ["Oracle AWR", "DB 連線池"]})
        self.assertEqual(todo, [{"item": "DB 連線池", "n": 2}, {"item": "Kibana log", "n": 1}, {"item": "Oracle AWR", "n": 0}])
        self.assertEqual(SIM.checklist(None, cases, {"db": ["x"]}), [])
        self.assertEqual(SIM.checklist("deploy", [], {}), [])

    def test_format(self):
        hints = SIM.segment_hints(FINDINGS, [case("a", slow=[DB], title="連線池耗盡")])
        text = "\n".join(SIM.format_hints(hints, [{"item": "DB 連線池", "n": 2}], {"category": "db"}))
        self.assertIn("歷史 case 的線索", text)
        self.assertIn("連線池耗盡", text)
        self.assertIn("- [ ] DB 連線池（過去 2 件有查）", text)
        self.assertEqual(SIM.format_hints([], [{"item": "x", "n": 0}], None), [])    # 沒分類不列清單

    def test_scenario_has_checklists(self):
        self.assertIn("db", load_scenario().checklists)


class AlertHook(unittest.TestCase):
    def test_case_hints_in_alert_flow(self):
        import incident.alert_service as C
        from incident.config.rca import Alerts

        class Inc:
            def __init__(self, rows=None, fail=False):
                self.rows, self.fail = rows or [], fail

            def find(self, statuses, **kw):
                if self.fail:
                    raise ConnectionError("down")
                return self.rows

        class Repo:
            scenario = load_scenario()
            incidents = Inc([case("a", slow=[DB], checked=["DB 連線池"])])

        findings = json.loads(json.dumps(FINDINGS))
        lines = C._case_hints(Repo(), Alerts(), findings, {"category": "db"})
        self.assertTrue(any("歷史 case 的線索" in l for l in lines))
        self.assertTrue(any("DB 連線池" in l for l in lines))
        self.assertEqual(findings["case_hints"][0]["cases"], ["a"])
        Repo.incidents = Inc(fail=True)
        self.assertEqual(C._case_hints(Repo(), Alerts(), dict(FINDINGS), None), [])      # 讀不到不擋
        self.assertEqual(C._case_hints(Repo(), Alerts(similar=False), dict(FINDINGS), None), [])


class Exam(unittest.TestCase):
    def investigate(self, results):
        def run(cfg, args):
            got = results[args.service]
            if isinstance(got, Exception):
                raise got
            return "", got
        return run

    def test_scoring(self):
        cases = [case("right", slow=["mes-api › SELECT WIP WHERE id=# → oracle"], services=["mes-api"]),
                 case("wrong", slow=["pay-api › redis"], services=["pay-api"]),
                 case("gone", slow=["old-api › x"], services=["old-api"]),
                 case("noans", services=["x-api"]),
                 case("boom", slow=["b › y"], services=["b"])]
        inv = self.investigate({"mes-api": FINDINGS, "pay-api": FINDINGS, "old-api": {"requests": {"requests": 0}}, "x-api": FINDINGS,
                                "b": RuntimeError("es down")})
        r = EVAL.exam(cases, inv, None)
        st = {x["id"]: x["status"] for x in r["rows"]}
        self.assertEqual(st, {"right": "right", "wrong": "wrong", "gone": "skipped", "noans": "no_answer", "boom": "skipped"})
        self.assertEqual((r["scored"], r["right"], r["accuracy"]), (2, 1, 0.5))

    def test_window_from_incident(self):
        seen = {}

        def inv(cfg, args):
            seen.update(start=args.start, end=args.end, service=args.service)
            return "", FINDINGS
        EVAL.exam([case("a", slow=[DB], start="2026-10-01T02:00:00Z")], inv, None)
        self.assertEqual(seen["service"], "mes-api")
        self.assertEqual(seen["start"], datetime(2026, 10, 1, 1, 55, tzinfo=timezone.utc))
        self.assertEqual(seen["end"], datetime(2026, 10, 1, 2, 35, tzinfo=timezone.utc))   # 沒有 end：start + 30 分 + 5 分

    def test_compare_and_run(self):
        cases = [case("a", slow=[DB]), case("b", slow=["mes-api › GET /lots"])]

        class Repo:
            class incidents:
                @staticmethod
                def find(statuses, having=()):
                    return cases

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "before.json")
            code, lines = EVAL.run(Repo, None, self.investigate({"mes-api": FINDINGS}), save=path)
            self.assertEqual(code, 0)
            self.assertIn("2/2", lines[-2])
            worse = {**FINDINGS, "segments": [FINDINGS["segments"][0]]}         # 改壞了：DB 那段不見了
            code, lines = EVAL.run(Repo, None, self.investigate({"mes-api": worse}), against=path)
            self.assertEqual(code, 1)
            self.assertTrue(any(l.strip().startswith("變差：a") for l in lines), lines)


if __name__ == "__main__":
    unittest.main()

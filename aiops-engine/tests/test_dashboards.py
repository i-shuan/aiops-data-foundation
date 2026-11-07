"""Grafana 儀表板與 data source 的對得上：每個面板、每個變數用的 data source uid 都有在 datasource.yaml 定義；整合儀表板的上半讀 APM 三種資料。
不連 Grafana、不連 ES、不連 PostgreSQL（對 Grafana 的實測見 docs/pdca/meta/2026-11-apm-overview-dashboard.md、2026-11-postgres-store.md）。"""
import json
import pathlib
import unittest

import yaml

import tests.apm_synth as S

FILES = pathlib.Path(__file__).resolve().parents[2] / "grafana" / "helmchart" / "files"
UIDS = {d["uid"] for d in yaml.safe_load((FILES / "datasource.yaml").read_text(encoding="utf-8"))["datasources"]}


def walk(panels):
    for p in panels:
        yield p
        yield from walk(p.get("panels", []))


class Dashboards(unittest.TestCase):
    def test_every_datasource_is_provisioned(self):
        for f in sorted((FILES / "dashboards").glob("*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            used = {(p.get("datasource") or {}).get("uid") for p in walk(d.get("panels", []))} - {None}
            used |= {t.get("datasource", {}).get("uid") for p in walk(d.get("panels", [])) for t in p.get("targets", [])} - {None}
            used |= {(v.get("datasource") or {}).get("uid") for v in d.get("templating", {}).get("list", [])} - {None}
            self.assertEqual(used - UIDS, set(), f"{f.name} 用了沒定義的 data source")

    def test_overview_reads_apm_then_aiops(self):
        d = json.loads((FILES / "dashboards" / "aiops-overview.json").read_text(encoding="utf-8"))
        ds = [(p["id"], p["datasource"]["uid"]) for p in d["panels"]]
        self.assertEqual([u for _, u in ds[:5]], ["apm-metrics", "apm-traces", "apm-traces", "apm-traces", "apm-logs"])
        self.assertTrue(all(u == "aiops-db" for _, u in ds[5:]))           # engine 自己的資料：PostgreSQL
        self.assertEqual([v["name"] for v in d["templating"]["list"]], ["service", "alert"])

    def test_aiops_data_is_postgres_and_every_query_is_sql(self):
        """engine 自己的資料搬到 PostgreSQL（docs/pdca/meta/2026-11-postgres-store.md）：一個 aiops-db，讀它的面板、變數、告警規則都是 SQL，
        表名是 aiops_*；ES 的 data source 只剩 APM 原始資料三個。SQL 能不能跑另外對真的 PostgreSQL、真的 Grafana 測過（PDCA 的 Check）。"""
        ds = {d["uid"]: d for d in yaml.safe_load((FILES / "datasource.yaml").read_text(encoding="utf-8"))["datasources"]}
        self.assertEqual(ds["aiops-db"]["type"], "grafana-postgresql-datasource")
        self.assertEqual({u for u, d in ds.items() if d["type"] == "elasticsearch"}, {"apm-metrics", "apm-traces", "apm-logs"})
        for f in sorted((FILES / "dashboards").glob("*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            for p in walk(d.get("panels", [])):
                if (p.get("datasource") or {}).get("uid") == "aiops-db":
                    for t in p["targets"]:
                        self.assertIn(" aiops_", t["rawSql"], f"{f.name} {p['title']}")
                        self.assertNotIn("aiops-", t["rawSql"])
        rules = yaml.safe_load((FILES / "alert-rules.yaml").read_text(encoding="utf-8"))["groups"][0]["rules"]
        for r in rules:
            self.assertEqual(r["data"][0]["datasourceUid"], "aiops-db", r["uid"])
            self.assertIn("FROM aiops_alerts", r["data"][0]["model"]["rawSql"])
        incident = next(r for r in rules if r["uid"] == "aiops-incident")["data"][0]["model"]["rawSql"]
        for label in ("alert_id", "dept", "group", "severity", "summary", "report_url"):      # 依部門分送靠這些 labels
            self.assertIn(f"AS {label}" if label != "group" else 'AS "group"', incident)

    def test_apm_datasources_match_the_engine_config(self):
        """儀表板讀的索引 pattern 要跟 engine 設定讀的一樣（configs/apm/）。"""
        cfg = {d["uid"]: d["jsonData"]["index"] for d in yaml.safe_load((FILES / "datasource.yaml").read_text(encoding="utf-8"))["datasources"]
               if d["type"] == "elasticsearch"}
        self.assertEqual(cfg["apm-metrics"], "metrics-apm.transaction.1m-*")
        self.assertEqual(cfg["apm-traces"], "traces-apm-*")
        self.assertTrue(cfg["apm-logs"].startswith("logs-apm."))


class ApmSynth(unittest.TestCase):
    def test_nest(self):
        self.assertEqual(S.nest({"a.b.c": 1, "a.d": 2, "e": 3}), {"a": {"b": {"c": 1}, "d": 2}, "e": 3})

    def test_histogram_counts_add_up(self):
        import random
        h = S.histogram(random.Random(1), 100, 70)
        self.assertEqual(sum(h["counts"]), 100)
        self.assertEqual(h["values"], sorted(h["values"]))

    def test_incident_is_slower_and_fails_more(self):
        import random
        rng = random.Random(3)
        normal = S.trace_docs(rng, __import__("datetime").datetime(2026, 1, 1), False, 0)[0]
        slow = S.trace_docs(rng, __import__("datetime").datetime(2026, 1, 1), True, 1)[0]
        get = lambda docs: max(d["transaction.duration.us"] for d in docs if d["processor.event"] == "transaction")
        self.assertGreater(get(slow), get(normal) * 5)


if __name__ == "__main__":
    unittest.main()

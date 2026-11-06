"""歷史事故單匯入（feedback.importer）：CSV／JSON 解析、缺欄位略過、指紋去重、時區、寫入的樣子。假的 ES，不用連線。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from shared.types.incident import INCIDENT_INDEX
import learn.history_importer as IMP
from shared.infra.repos import Repos
from shared.scenario import load_scenario

SC = load_scenario()
import llm.similar as SIM
from interface.cli.rca import main

HERE = Path(__file__).resolve().parent
ROW = {"title": "Oracle 統計資訊過期", "start": "2026-03-12 02:10", "end": "2026-03-12 03:05", "services": "mes-api,wip-worker",
       "root_cause_category": "db", "root_cause": "統計資訊過期", "action": "重跑 analyze", "how_detected": "AWR 執行計畫變了",
       "checked": "Oracle AWR / 慢查詢,trace 瀑布圖", "by": "王大明"}


def write_csv(dirpath: str, rows: list[dict], name="in.csv", bom=True) -> str:
    import csv
    p = Path(dirpath) / name
    keys = list({k: 1 for r in rows for k in r})
    with open(p, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    return str(p)


def write_json(dirpath: str, data, name="in.json") -> str:
    p = Path(dirpath) / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


class FakeES:
    def __init__(self):
        self.indexed, self.ensured = [], []

    def ensure_index(self, name, body):
        self.ensured.append((name, body))

    def bulk_index(self, index, docs):
        self.indexed.append((index, list(docs)))


class ParseCSV(unittest.TestCase):
    def test_csv_with_bom_lists_and_timezone(self):
        with tempfile.TemporaryDirectory() as d:
            docs, skipped = IMP.parse(write_csv(d, [ROW]))
        self.assertEqual(skipped, [])
        (_id, doc), = docs
        self.assertEqual(doc["services"], ["mes-api", "wip-worker"])                      # 逗號分隔 → 陣列
        self.assertEqual(doc["checked"], ["Oracle AWR / 慢查詢", "trace 瀑布圖"])
        self.assertEqual(doc["start"], "2026-03-12T02:10:00+08:00")                      # 沒寫時區當台北
        self.assertEqual(doc["end"], "2026-03-12T03:05:00+08:00")
        self.assertEqual((doc["status"], doc["source"]), ("confirmed", "import"))
        self.assertEqual(doc["alert_id"], "imp-20260312-01")
        self.assertEqual((doc["by"], doc["confirmed_by"]), ("王大明", "王大明"))
        self.assertNotIn("note", doc)                                                      # 沒給的選填欄位不寫

    def test_pipe_separator_when_value_has_comma(self):
        row = {**ROW, "slow_segments": "wip-worker › Oracle SELECT a, b FROM lots 123|mes-api › GET → wip-worker", "routes": "GET /lots/{id}"}
        with tempfile.TemporaryDirectory() as d:
            (_, doc), = IMP.parse(write_csv(d, [row]))[0]
        fp = doc["fingerprint"]
        self.assertEqual(fp["slow_segments"], ["wip-worker › Oracle SELECT a, b FROM lots #", "mes-api › GET → wip-worker"])   # 去數字，跟自動的對得上
        self.assertEqual(fp["routes"], ["GET /lots/{id}"])
        self.assertEqual(fp["services"], ["mes-api", "wip-worker"])
        self.assertEqual((fp["series"], fp["error_origins"], fp["patterns"]), ([], [], []))

    def test_header_whitespace_tolerated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.csv"
            p.write_text("title , start,end,services,root_cause_category,root_cause,action,how_detected,checked\n"
                         "t,2026-01-01 10:00,2026-01-01 11:00,svc,db,rc,act,how,chk\n", encoding="utf-8")
            docs, skipped = IMP.parse(p)
        self.assertEqual(skipped, [])
        self.assertEqual(docs[0][1]["title"], "t")


class ParseJSON(unittest.TestCase):
    def test_array_with_list_fields(self):
        row = {**ROW, "services": ["wip-worker", "mes-api"], "checked": ["DB 連線池"], "routes": ["GET /lots/{id}"], "note": "備註"}
        with tempfile.TemporaryDirectory() as d:
            docs, skipped = IMP.parse(write_json(d, [row]))
        self.assertEqual(skipped, [])
        doc = docs[0][1]
        self.assertEqual(doc["services"], ["mes-api", "wip-worker"])     # 排序過：順序不同不算不同的事故
        self.assertEqual(doc["checked"], ["DB 連線池"])
        self.assertEqual(doc["note"], "備註")

    def test_wrapped_object_and_bad_shape(self):
        with tempfile.TemporaryDirectory() as d:
            docs, _ = IMP.parse(write_json(d, {"incidents": [ROW]}))
            self.assertEqual(len(docs), 1)
            with self.assertRaises(SystemExit):
                IMP.parse(write_json(d, {"foo": "bar"}, name="bad.json"))


class Skip(unittest.TestCase):
    def test_missing_required_fields_skipped_others_imported(self):
        rows = [ROW, {**ROW, "title": "沒處置", "action": "", "how_detected": " "}, {**ROW, "title": "沒服務", "services": ""}]
        with tempfile.TemporaryDirectory() as d:
            docs, skipped = IMP.parse(write_csv(d, rows))
        self.assertEqual(len(docs), 1)
        self.assertEqual(skipped, [(2, "缺 action、how_detected"), (3, "缺 services")])

    def test_bad_time_and_end_before_start(self):
        rows = [{**ROW, "start": "昨天晚上"}, {**ROW, "end": "2026-03-12 01:00"}]
        with tempfile.TemporaryDirectory() as d:
            docs, skipped = IMP.parse(write_csv(d, rows))
        self.assertEqual(docs, [])
        self.assertEqual(skipped[0][0], 1)
        self.assertIn("時間看不懂：start「昨天晚上」", skipped[0][1])
        self.assertEqual(skipped[1][0], 2)
        self.assertIn("早於 start", skipped[1][1])

    def test_optional_fields_not_required(self):
        row = {k: v for k, v in ROW.items() if k != "by"}
        self.assertIsNone(IMP.normalize(row, 1)[1])


class Dedupe(unittest.TestCase):
    def test_same_content_same_id(self):
        a, _ = IMP.normalize(ROW, 1)
        b, _ = IMP.normalize({**ROW, "services": "wip-worker, mes-api", "action": "改了處置"}, 7)   # 服務順序、處置不同：同一件，覆蓋
        c, _ = IMP.normalize({**ROW, "title": "另一件"}, 1)
        self.assertEqual(a[0], b[0])
        self.assertNotEqual(a[0], c[0])
        self.assertEqual(b[1]["alert_id"], "imp-20260312-07")        # alert_id 只是檔案裡第幾筆，不影響 _id

    def test_rerun_same_file_same_ids(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_csv(d, [ROW, {**ROW, "title": "第二件"}])
            ids1 = [i for i, _ in IMP.parse(p)[0]]
            ids2 = [i for i, _ in IMP.parse(p)[0]]
        self.assertEqual(ids1, ids2)
        self.assertEqual(len(set(ids1)), 2)


class Timezone(unittest.TestCase):
    def test_explicit_offset_kept_and_other_tz(self):
        (_, utc), _ = IMP.normalize({**ROW, "start": "2026-03-11T18:10:00Z", "end": "2026-03-11T19:05:00+00:00"}, 1)
        self.assertEqual(utc["start"], "2026-03-11T18:10:00+00:00")   # 有寫時區就照寫的
        self.assertEqual(utc["alert_id"], "imp-20260312-01")           # 日期用台北的（UTC 11 日 18 點＝台北 12 日）
        (_, tokyo), _ = IMP.normalize(ROW, 1, tz="Asia/Tokyo")
        self.assertEqual(tokyo["start"], "2026-03-12T02:10:00+09:00")

    def test_date_only(self):
        (_, doc), _ = IMP.normalize({**ROW, "start": "2026-03-12", "end": "2026-03-13"}, 1)
        self.assertEqual(doc["start"], "2026-03-12T00:00:00+08:00")


class WriteAndSearch(unittest.TestCase):
    def test_write_ensures_mapping_and_bulk_indexes(self):
        es = FakeES()
        docs, _ = IMP.parse(HERE.parent / "configs/rca/incidents-example.csv")
        self.assertEqual(IMP.write(Repos(es, SC), docs), 10)
        (name, body), = es.ensured
        self.assertEqual(name, INCIDENT_INDEX)
        self.assertIn("source", body["mappings"]["properties"])
        (index, items), = es.indexed
        self.assertEqual(index, INCIDENT_INDEX)
        self.assertEqual(len(items), 10)
        self.assertEqual(IMP.write(Repos(es, SC), []), 0)

    def test_imported_doc_is_found_by_similar_search(self):
        """匯入的文件形狀要能被 similar.search 拿到：status confirmed、fingerprint 跟自動的同一個形狀。"""
        from tests.test_similar import FakeES as SimES
        docs, _ = IMP.parse(HERE.parent / "configs/rca/incidents-example.csv")
        es = SimES(dict(docs))
        fp = SIM.fingerprint({"service": "wip-worker", "routes": ["GET /lots/{id}"], "series": ["http.latency_p95_s"]},
                             {"segments": [{"segment": "wip-worker › Oracle SELECT LOT_HISTORY", "delta_ms": 900}]})
        got = SIM.search(Repos(es, SC), fp)
        self.assertTrue(got)
        self.assertEqual(got[0]["root_cause_category"], "db")
        self.assertIn("同一段變慢（wip-worker › Oracle SELECT LOT_HISTORY）", got[0]["why"])


class Files(unittest.TestCase):
    def test_example_and_synthetic_files_are_complete(self):
        """範例檔每筆都齊；合成檔 20 筆、每個根因分類 ≥ 2（留一法才算得出來）。"""
        from collections import Counter
        ex, skipped = IMP.parse(HERE.parent / "configs/rca/incidents-example.csv")
        self.assertEqual((len(ex), skipped), (10, []))
        syn, skipped = IMP.parse(HERE / "incidents-synthetic.csv")
        self.assertEqual((len(syn), skipped), (20, []))
        self.assertEqual(len({i for i, _ in syn}), 20)
        cats = Counter(d["root_cause_category"] for _, d in syn)
        self.assertTrue(all(n >= 2 for n in cats.values()), cats)
        self.assertEqual([d["title"] for _, d in ex], [d["title"] for _, d in syn][:10])   # 合成檔是範例檔擴充


class CLI(unittest.TestCase):
    def test_dry_run_prints_counts_without_es(self):
        rows = [ROW, {**ROW, "title": "缺的", "checked": ""}]
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            rc = main(["import-incidents", write_csv(d, rows), "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("第 2 筆略過：缺 checked", out.getvalue())
        self.assertIn("1 筆可匯入、1 筆略過（共 2 筆", out.getvalue())


if __name__ == "__main__":
    unittest.main()

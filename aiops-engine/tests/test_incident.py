"""shared/incident_doc.py：Fingerprint／Incident 的 to_doc() 跟改成類別之前（master 31c72f8）四條寫入路徑手寫的 dict 比；欄位跟 INCIDENT_MAPPING、
similar.WEIGHTS 對得上；欄位名打錯、status 給錯會擋；from_doc 讀舊文件（缺欄位、多欄位、型別不對）不炸。不連 ES。

期望值是改之前用同樣輸入跑 similar.fingerprint／importer.normalize／cmd_alert._incident_doc 存下來的（docs/pdca/meta/2026-11-vo-models.md Check）。"""
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from incident.store.models import Alert
from shared.incident_doc import INCIDENT_MAPPING, UNMAPPED_FIELDS, Fingerprint, Incident
import learn.importer as IMP
import llm.similar as SIM

ALERT = {"alert_id": "a-1", "service": "mes-api", "routes": ["GET /lots/{id}"], "series": ["http.latency_p95_s"]}
FINDINGS = {"segments": [{"segment": "mes-api › wip-worker › Oracle SELECT lots 123", "delta_ms": 900.0, "now_ms": 1000, "ref_ms": 100},
                         {"segment": "x › y", "delta_ms": -1}],
            "error_origins": [{"segment": "wip-worker › Kafka produce 7", "now_errors": 40, "ref_errors": 0}],
            "new_patterns": [{"pattern": "p" * 200, "is_new": True}, {"pattern": "old", "is_new": False}], "route": "GET /r"}
NOW = datetime(2026, 11, 6, 1, 2, 3, tzinfo=timezone.utc)
ROW = {"title": "lots 變慢", "start": "2026-03-12 02:10", "end": "2026-03-12 03:00", "services": "mes-api|wip-worker", "root_cause_category": "db",
       "root_cause": "統計過期", "action": "重跑 stats", "how_detected": "AWR", "checked": "AWR|trace", "by": "老王", "note": "",
       "slow_segments": "mes-api › SELECT 123", "patterns": "x"}


class FingerprintGolden(unittest.TestCase):
    def test_from_alert_with_findings(self):
        # 服務：告警的＋最慢三段的第一個服務（x › y 雖然沒變慢也算）；慢的段只留 delta_ms > 0、去數字；log 樣式截 120 字、只留 is_new
        self.assertEqual(SIM.fingerprint(ALERT, FINDINGS).to_doc(), {
            "services": ["mes-api", "x"], "routes": ["GET /lots/{id}"], "series": ["http.latency_p95_s"],
            "slow_segments": ["mes-api › wip-worker › Oracle SELECT lots #"], "error_origins": ["wip-worker › Kafka produce #"],
            "patterns": ["p" * 120]})

    def test_from_alert_without_findings(self):
        self.assertEqual(SIM.fingerprint(ALERT, None).to_doc(), {
            "services": ["mes-api"], "routes": ["GET /lots/{id}"], "series": ["http.latency_p95_s"],
            "slow_segments": [], "error_origins": [], "patterns": []})

    def test_route_and_series_fall_back_to_findings(self):
        fp = SIM.fingerprint({"service": "s"}, {"route": "GET /r", "anomalies": [{"series": "b"}, {"series": "a"}, {}]})
        self.assertEqual(fp.to_doc(), {"services": ["s"], "routes": ["GET /r"], "series": ["a", "b"],
                                       "slow_segments": [], "error_origins": [], "patterns": []})

    def test_importer_and_alert_share_normalization(self):
        """匯入的和自動算的經同一個 normalized：去數字、最多 3 段、樣式截 120 字最多 5 個。"""
        fp = IMP.fingerprint({"slow_segments": "a 1|b 2|c 3|d 4", "patterns": ",".join(["q" * 130] * 6), "routes": "r,r"}, ["s"])
        self.assertEqual(fp.slow_segments, ["a #", "b #", "c #"])
        self.assertEqual(fp.patterns, ["q" * 120] * 5)
        self.assertEqual(fp.routes, ["r"])

    def test_weights_cover_exactly_the_fields(self):
        self.assertEqual(set(SIM.WEIGHTS), set(Fingerprint.FIELDS))
        self.assertEqual(set(Fingerprint.FIELDS), set(Fingerprint.model_fields))
        self.assertEqual(set(INCIDENT_MAPPING["mappings"]["properties"]["fingerprint"]["properties"]), set(Fingerprint.FIELDS))

    def test_of_and_empty(self):
        self.assertTrue(Fingerprint.of(None).empty)
        self.assertTrue(Fingerprint.of({}).empty)
        self.assertFalse(Fingerprint.of({"services": ["a"]}).empty)
        fp = Fingerprint(services=["a"])
        self.assertIs(Fingerprint.of(fp), fp)

    def test_tolerant_read(self):
        """ES 回來的 keyword 可能是單值、None；多出來的鍵忽略；元素裡的 None 丟掉。"""
        fp = Fingerprint.from_doc({"services": "mes-api", "routes": None, "series": ["a", None], "legacy": 1})
        self.assertEqual((fp.services, fp.routes, fp.series), (["mes-api"], [], ["a"]))

    def test_typo_is_rejected(self):
        with self.assertRaises(ValidationError):
            Fingerprint(service=["a"])


class IncidentGolden(unittest.TestCase):
    def test_import_row(self):
        (_id, doc), why = IMP.normalize(ROW, 3, now=NOW)
        self.assertIsNone(why)
        self.assertEqual(_id, "3dd847a66e853a6ccb9df1f50da45199cec4cc25")      # 內容指紋沒變：重匯才會覆蓋同一筆
        self.assertEqual(doc, {
            "@timestamp": "2026-11-06T01:02:03+00:00", "title": "lots 變慢", "start": "2026-03-12T02:10:00+08:00", "end": "2026-03-12T03:00:00+08:00",
            "services": ["mes-api", "wip-worker"], "status": "confirmed", "source": "import",
            "detected_at": "2026-03-12T02:10:00+08:00", "resolved_at": "2026-03-12T03:00:00+08:00",
            "root_cause_category": "db", "root_cause": "統計過期", "action": "重跑 stats", "how_detected": "AWR", "checked": ["AWR", "trace"],
            "fingerprint": {"services": ["mes-api", "wip-worker"], "routes": [], "series": [], "slow_segments": ["mes-api › SELECT #"],
                            "error_origins": [], "patterns": ["x"]},
            "by": "老王", "confirmed_by": "老王", "alert_id": "imp-20260312-03"})

    def test_import_row_minimal(self):
        (_id, doc), _ = IMP.normalize({k: v for k, v in ROW.items() if k in IMP.REQUIRED}, 1, now=NOW)
        for absent in ("by", "confirmed_by", "note", "anomaly_ids", "upstream_services"):
            self.assertNotIn(absent, doc)       # 選填沒給就不寫（跟改之前一樣）；匯入沒碰的陣列欄位（anomaly_ids…）也不會多出來
        self.assertEqual(doc["alert_id"], "imp-20260312-01")
        self.assertEqual(doc["fingerprint"]["slow_segments"], [])

    def test_import_row_rejected_reasons_unchanged(self):
        self.assertEqual(IMP.normalize({**ROW, "end": "2026-01-01"}, 1, now=NOW),
                         (None, "end（2026-01-01T00:00:00+08:00）早於 start（2026-03-12T02:10:00+08:00）"))

    AL = Alert(alert_id="a-1", category="service", service="wip-worker", status="open", started_at=NOW, updated_at=NOW,
               upstream_services=["mes-api"], severity="critical", anomaly_ids=["x1", "x2"], routes=[], series=[], merged_into="a-9")
    DOC = {"@timestamp": "t", "started_at": "s", "updated_at": "u", "title": "x", "dept": None, "group": "g"}

    def test_auto_incident(self):
        got = Incident.from_alert(self.AL, self.DOC, None).to_doc()
        # 改之前多一個 "dept": None；現在值是 None 的不寫（ES 的 null 和沒有這個欄位對查詢、對 .get 都一樣）
        self.assertEqual(got, {"@timestamp": "t", "alert_id": "a-1", "title": "x", "start": "s", "end": "u", "services": ["wip-worker"],
                               "severity": "critical", "upstream_services": ["mes-api"], "status": "auto", "group": "g",
                               "anomaly_ids": ["x1", "x2"], "detected_at": "s",
                               "fingerprint": {"services": ["wip-worker"], "routes": [], "series": [], "slow_segments": [], "error_origins": [],
                                               "patterns": []}})

    def test_merged_incident(self):
        got = Incident.from_alert(self.AL, {**self.DOC, "resolved_at": "r", "title": None}, FINDINGS, status="merged", merged_into="a-9").to_doc()
        self.assertEqual((got["status"], got["merged_into"], got["end"]), ("merged", "a-9", "r"))
        self.assertNotIn("title", got)
        self.assertEqual(got["fingerprint"]["services"], ["mes-api", "wip-worker", "x"])
        self.assertEqual(got["anomaly_ids"], ["x1", "x2"])

    def test_anomaly_ids_capped(self):
        al = self.AL.model_copy(update={"anomaly_ids": [str(i) for i in range(500)]})
        self.assertEqual(len(Incident.from_alert(al, self.DOC, None).anomaly_ids), Incident.MAX_ANOMALY_IDS)

    def test_manual_incident_drops_blank(self):
        """人工補登：沒給的（None）、空字串不寫；空陣列會寫成 []（改之前不寫，讀的人都是 .get(...) or []，沒差）。"""
        doc = Incident(**{"@timestamp": "t"}, title="x", start="s", end="e", services=[], severity=None, status="open",
                       root_cause="", action=None, by=None, anomaly_ids=[]).to_doc()
        self.assertEqual(doc, {"@timestamp": "t", "status": "open", "title": "x", "start": "s", "end": "e", "services": [], "anomaly_ids": []})

    def test_seed_drops_none_service(self):
        """回饋網頁的種子：告警沒有 service 時以前會寫 [None]，現在丟掉。"""
        self.assertEqual(Incident(**{"@timestamp": "t"}, status="confirmed", services=[None]).services, [])

    def test_mapping_fields_are_model_fields(self):
        names = {f.alias or n for n, f in Incident.model_fields.items()}
        mapped = set(INCIDENT_MAPPING["mappings"]["properties"])
        self.assertEqual(mapped - names, set(), "mapping 有、模型沒有的欄位")
        self.assertEqual(names - mapped, set(UNMAPPED_FIELDS), "模型有、mapping 沒有的欄位要列在 UNMAPPED_FIELDS")

    def test_typo_and_bad_status_rejected(self):
        with self.assertRaises(ValidationError):
            Incident(**{"@timestamp": "t"}, status="auto", titel="x")
        with self.assertRaises(ValidationError):
            Incident(**{"@timestamp": "t"}, status="confimed")
        inc = Incident(**{"@timestamp": "t"}, status="auto")
        with self.assertRaises(ValidationError):
            inc.status = "done"

    def test_from_doc_old_documents(self):
        """舊文件：缺大半欄位、多了不認得的鍵、services 是單值、fingerprint 不完整、severity 是數字——都讀得進來。"""
        inc = Incident.from_doc({"status": "confirmed", "title": "舊", "services": "mes-api", "severity": 3, "legacy_field": True,
                                 "fingerprint": {"services": ["mes-api"]}})
        self.assertEqual((inc.timestamp, inc.services, inc.severity, inc.anomaly_ids), (None, ["mes-api"], "3", []))
        self.assertEqual(inc.fingerprint.routes, [])
        self.assertIsNone(Incident.from_doc({"status": "auto", "fingerprint": "garbage"}).fingerprint)
        self.assertEqual(Incident.from_doc({"status": "auto", "title": ""}).title, None)

    def test_roundtrip(self):
        (_id, doc), _ = IMP.normalize(ROW, 3, now=NOW)
        self.assertEqual(Incident.from_doc(doc).to_doc(), doc)


if __name__ == "__main__":
    unittest.main()

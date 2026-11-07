"""incident/store/models.py：Alert 是 ⑥ 併件時手上的物件、寫進 aiops-alerts 的格式、讀回來的格式。
執行期欄位（anoms、new…）不寫 ES；時間欄位物件裡是 datetime、to_doc 轉 ISO 字串；from_doc 讀舊文件寬鬆；打錯欄位名會擋。
to_doc 跟改成類別之前 alert_doc 寫的 dict 的逐欄位比對記在 docs/pdca/meta/2026-11-clean-code.md Check。"""
import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from incident.store.models import Alert

T0 = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)


def alert(**kw) -> Alert:
    base = dict(alert_id="a-1", category="service", service="mes-api", status="open", started_at=T0, updated_at=T0 + timedelta(minutes=5))
    return Alert(**{**base, **kw})


class Contract(unittest.TestCase):

    def test_runtime_fields_never_written(self):
        al = alert(anoms=[{"_id": "x1"}], new=True, upgraded=True, rerun=True, report_text="r", llm={"text": "t"})
        doc = al.to_doc()
        for f in ("anoms", "new", "upgraded", "rerun", "report_text", "llm"):
            self.assertNotIn(f, doc)
        self.assertEqual(doc["alert_id"], "a-1")

    def test_times_are_datetimes_inside_and_iso_outside(self):
        al = Alert.from_doc({"alert_id": "a", "category": "service", "service": "s", "status": "open",
                             "started_at": "2026-10-25T02:00:00Z", "updated_at": "2026-10-25T02:05:00+00:00", "report_at": "2026-10-25T02:06:00+00:00"})
        self.assertEqual(al.started_at, T0)                                  # "Z" 跟 "+00:00" 都收
        self.assertEqual(al.updated_at - al.started_at, timedelta(minutes=5))
        d = al.to_doc()
        self.assertEqual(d["started_at"], "2026-10-25T02:00:00+00:00")       # 寫出去是 ISO 字串（跟以前 isoformat 一樣）
        self.assertEqual(d["report_at"], "2026-10-25T02:06:00+00:00")
        self.assertNotIn("resolved_at", d)                                   # 沒給的不寫

    def test_from_doc_is_lenient_and_roundtrips(self):
        src = {"@timestamp": "2026-10-25T02:11:00+00:00", "alert_id": "a", "category": "service", "service": "s", "status": "open",
               "started_at": "2026-10-25T02:00:00+00:00", "updated_at": "2026-10-25T02:05:00+00:00", "series": None, "similar": None,
               "kinds": ["baseline"], "rca_requested": True}
        al = Alert.from_doc(src)
        self.assertEqual((al.series, al.similar, al.rca_requested, al.timestamp.isoformat()), ([], [], True, "2026-10-25T02:11:00+00:00"))
        back = al.to_doc()
        self.assertNotIn("kinds", back)                                      # 不認得的舊欄位：讀時忽略、不帶回去
        self.assertEqual(back["series"], [])                                  # None 進來、[] 出去（讀的程式都 or []）
        self.assertEqual({k: v for k, v in back.items() if k not in ("series", "similar")},
                         {k: v for k, v in src.items() if k not in ("series", "similar", "kinds")})

    def test_typos_and_bad_status_rejected(self):
        with self.assertRaises(ValidationError):
            alert(servcie="x")
        with self.assertRaises(ValidationError):
            alert(status="closed")
        al = alert()
        with self.assertRaises(ValidationError):
            al.status = "done"
        with self.assertRaises(ValidationError):
            Alert.from_doc({"alert_id": "a", "status": "open"})              # 缺 category、service、時間：寫入的四條路都會給，讀到這種是壞資料


if __name__ == "__main__":
    unittest.main()

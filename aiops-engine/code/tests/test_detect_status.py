"""detect 每輪寫 detect-status 進 aiops-alerts：missing_baseline 是沒有 baseline 的序列數（docs/pdca/meta/2026-10-open-issues.md 第 7 節）。
假的 ES：aiops-baseline 只對指定的序列回 baseline，其他查詢都回空。"""
import contextlib
import io
import unittest
from argparse import Namespace
from unittest import mock

from detector import cmd_baseline as C
from investigator.cmd_alert import ALERT_INDEX, ALERT_MAPPING

CFG = {"interval": "5m", "adjustments_file": None, "calendar_file": None, "detect": {"z": 5}, "status": {"confirm_points": 2},
       "series": [{"name": "a.cpu", "index": "m", "by": ["service"], "value": {"avg": "v"}},
                  {"name": "b.mem", "index": "m", "by": ["service"], "value": {"avg": "v"}}]}

BASELINE = {"keys": {"service": "s1"}, "dow": 0, "hour": 0, "level": "all", "median": 1.0, "mad": 0.1, "resolution": "5m",
            "method": "mad", "baseline_source": "self", "trained_at": "2026-10-31T02:00:00Z"}


class FakeES:
    def __init__(self, with_baseline):
        self.with_baseline, self.ensured, self.written = with_baseline, [], []

    def ensure_index(self, name, body):
        self.ensured.append((name, body))

    def req(self, method, path, body=None, ok=None):
        if path.startswith("/aiops-baseline/_search"):
            series = body["query"]["bool"]["filter"][0]["term"]["series"]
            return {"hits": {"hits": [{"_source": {**BASELINE, "series": series}}] if series in self.with_baseline else []}}
        return {}

    def bulk_index(self, index, docs):
        self.written.append((index, docs))


def detect(with_baseline):
    es = FakeES(with_baseline)
    with mock.patch.object(C, "ES", lambda: es), contextlib.redirect_stdout(io.StringIO()) as out:
        C.cmd_detect(CFG, Namespace(at="2026-11-01T02:05:00Z", window="10min"))
    status = [d for idx, docs in es.written if idx == ALERT_INDEX for d in docs]
    return es, status, out.getvalue()


class DetectStatus(unittest.TestCase):
    def test_one_series_without_baseline(self):
        es, status, text = detect({"a.cpu"})
        self.assertEqual(len(status), 1)
        _id, doc = status[0]
        self.assertEqual(_id, "detect-status")
        self.assertEqual((doc["alert_id"], doc["category"], doc["status"], doc["service"]), ("detect-status", "status", "ok", "aiops"))
        self.assertEqual((doc["missing_baseline"], doc["series"]), (1, ["b.mem"]))
        # 跟心跳一樣的形狀：事件整合查 status:open、儀表板查 category:service 都不會抓到它
        self.assertEqual((doc["anomaly_ids"], doc["anomaly_count"], doc["routes"], doc["group"]), ([], 0, [], "unassigned"))
        self.assertLess(doc["@timestamp"], doc["updated_at"])      # @timestamp 往前 1 分鐘（STAMP_LAG），Grafana 的窗才查得到
        # 既有的 aiops-alerts 要補 missing_baseline 欄位（dynamic false）
        self.assertIn((ALERT_INDEX, ALERT_MAPPING), es.ensured)
        self.assertEqual(ALERT_MAPPING["mappings"]["properties"]["missing_baseline"], {"type": "integer"})
        self.assertIn("b.mem：沒有 baseline", text)
        self.assertIn("1 條序列沒有 baseline", text)

    def test_zero_missing_still_written(self):
        # 都有 baseline 也要寫（missing_baseline 0）：Grafana 才看得到「這輪正常」，規則的 noDataState 是 OK 不是 Alerting
        _, status, _ = detect({"a.cpu", "b.mem"})
        self.assertEqual(len(status), 1)
        self.assertEqual((status[0][1]["missing_baseline"], status[0][1]["series"]), (0, []))

    def test_all_missing(self):
        _, status, _ = detect(set())
        self.assertEqual((status[0][1]["missing_baseline"], status[0][1]["series"]), (2, ["a.cpu", "b.mem"]))


if __name__ == "__main__":
    unittest.main()

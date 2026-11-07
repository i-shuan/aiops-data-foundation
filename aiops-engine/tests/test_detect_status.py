"""detect 每輪寫 detect-status 進告警（PostgreSQL 的 aiops_alerts）：missing_baseline 是沒有 baseline 的序列數（docs/pdca/meta/2026-10-open-issues.md 第 7 節）。
基準線先寫進測試用的 PostgreSQL（tests/pg.py），只有指定的序列有；原始資料用假的 ES、都回空。"""
import contextlib
import io
import unittest

import pandas as pd
from argparse import Namespace

import detect.detect_service as C
from common.config import validate
from common.infra.repos import Repos
from common.scenario import load_scenario
from tests.pg import fresh_db, needs_pg

CFG = validate({"interval": "5m", "adjustments_file": None, "calendar_file": None, "detect": {"z": 5}, "status": {"confirm_points": 2},
       "series": [{"name": "a.cpu", "index": "m", "by": ["service"], "value": {"avg": "v"}},
                  {"name": "b.mem", "index": "m", "by": ["service"], "value": {"avg": "v"}}]}, "series")

BASELINE = {"keys": {"service": "s1"}, "dow": 0, "hour": 0, "level": "all", "median": 1.0, "mad": 0.1, "resolution": "5m",
            "method": "mad", "baseline_source": "self", "trained_at": "2026-10-31T02:00:00Z"}


class FakeES:
    """原始資料：序列查詢都回空（這裡只看 detect-status）。"""
    def search(self, index, body):
        return {}

    def req(self, method, path, body=None, ok=None):
        return {}


def detect(with_baseline):
    db = fresh_db()
    repo = Repos(FakeES(), load_scenario(), db=db)
    repo.baseline.bulk([(f"b-{s}", {**BASELINE, "series": s}) for s in sorted(with_baseline)])
    with contextlib.redirect_stdout(io.StringIO()) as out:
        C.cmd_detect(CFG, Namespace(at=pd.Timestamp("2026-11-01T02:05:00Z"), window=pd.Timedelta("10min")), repo)
    got = repo.alerts.get("detect-status")
    return repo, ([("detect-status", got)] if got else []), out.getvalue()     # source-check-* 那兩筆另外測（test_source_check.py）


@needs_pg
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

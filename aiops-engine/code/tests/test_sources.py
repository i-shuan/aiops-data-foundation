"""資料來源的介面（detector.sources）：file、http、taiwan；失敗不擋住指令。不連網路。"""
import json
import os
import tempfile
import unittest

import pandas as pd

from detector.adjust import adjust as A
from sources import providers as S

NOW = pd.Timestamp("2026-10-24T00:00:00Z")


def day(date, week, is_holiday, desc=""):
    return {"date": date, "week": week, "isHoliday": is_holiday, "description": desc}


# 擷取 2026 的形狀：國定假日、一般週末、平日、補班（這個來源 2026 沒有補班，用假的一天驗格式）
YEAR_2026 = [day("20260101", "四", True, "開國紀念日"), day("20260102", "五", False), day("20260103", "六", True),
             day("20260104", "日", True), day("20260216", "一", True, "農曆除夕"), day("20260221", "六", False, "補行上班")]


def fetcher(years: dict, calls=None):
    def fetch(url, headers=None, timeout=20):
        if calls is not None:
            calls.append(url)
        y = int(url.rsplit("/", 1)[1].split(".")[0])
        if y not in years:
            return None
        if years[y] == "boom":
            raise ConnectionError("no network")
        return years[y]
    return fetch


class ParseTaiwan(unittest.TestCase):
    def test_holiday_weekend_makeup(self):
        got = S.parse_taiwan(YEAR_2026)
        self.assertEqual(got, [{"date": "2026-01-01", "name": "開國紀念日", "type": "holiday"},
                               {"date": "2026-02-16", "name": "農曆除夕", "type": "holiday"},
                               {"date": "2026-02-21", "name": "補行上班", "type": "makeup_workday"}])

    def test_empty_and_missing_description(self):
        self.assertEqual(S.parse_taiwan([]), [])
        self.assertEqual(S.parse_taiwan([{"date": "20260105", "isHoliday": True, "description": None}]), [])

    def test_becomes_exclude_window_in_local_time(self):
        adjs = A.calendar_adjustments(S.parse_taiwan(YEAR_2026), "Asia/Taipei")
        first = adjs[0]
        self.assertEqual(pd.Timestamp(first["start"]), pd.Timestamp("2026-01-01T00:00:00+08:00"))
        self.assertEqual(pd.Timestamp(first["end"]), pd.Timestamp("2026-01-02T00:00:00+08:00"))
        self.assertEqual(first["apply_to"], ["train", "detect"])   # 補班日也是：不學、不報


class TaiwanCalendar(unittest.TestCase):
    def test_years_and_missing_year_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            got = S.taiwan_calendar([2026, 2027], cache_dir=d, fetch=fetcher({2026: YEAR_2026}, calls))
            self.assertEqual(len(got), 3)
            self.assertEqual(len(calls), 2)
            self.assertTrue(os.path.exists(os.path.join(d, "taiwan-2026.json")))
            self.assertFalse(os.path.exists(os.path.join(d, "taiwan-2027.json")))   # 404 不當成資料存

    def test_network_failure_uses_cache_even_if_old(self):
        with tempfile.TemporaryDirectory() as d:
            S.taiwan_calendar([2026], cache_dir=d, fetch=fetcher({2026: YEAR_2026}))
            old = os.path.join(d, "taiwan-2026.json")
            os.utime(old, (0, 0))   # 很舊的快取：過期了，會去抓；抓失敗就用它
            got = S.taiwan_calendar([2026], cache_dir=d, fetch=fetcher({2026: "boom"}))
            self.assertEqual(len(got), 3)

    def test_network_failure_no_cache_is_empty_not_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(S.taiwan_calendar([2026], cache_dir=d, fetch=fetcher({2026: "boom"})), [])

    def test_fresh_cache_means_no_network(self):
        # detect 每 30 秒跑一次：快取還新鮮就不能再打網路
        with tempfile.TemporaryDirectory() as d:
            calls = []
            S.taiwan_calendar([2026], cache_dir=d, fetch=fetcher({2026: YEAR_2026}, calls))
            for _ in range(5):
                got = S.taiwan_calendar([2026], cache_dir=d, fetch=fetcher({2026: YEAR_2026}, calls))
            self.assertEqual((len(calls), len(got)), (1, 3))

    def test_expired_cache_refetches(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            S.taiwan_calendar([2026], cache_dir=d, fetch=fetcher({2026: YEAR_2026}, calls))
            os.utime(os.path.join(d, "taiwan-2026.json"), (0, 0))
            S.taiwan_calendar([2026], cache_dir=d, fetch=fetcher({2026: YEAR_2026}, calls))
            self.assertEqual(len(calls), 2)

    def test_ttl_zero_always_fetches(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            for _ in range(3):
                S.taiwan_calendar([2026], cache_dir=d, ttl=0, fetch=fetcher({2026: YEAR_2026}, calls))
            self.assertEqual(len(calls), 3)

    def test_year_not_published_is_not_asked_every_time(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            for _ in range(4):
                S.taiwan_calendar([2028], cache_dir=d, fetch=fetcher({}, calls))
            self.assertEqual(len(calls), 1)   # 404 也記下來，ttl 內不再問

    def test_year_published_later_is_picked_up_after_ttl(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(S.taiwan_calendar([2028], cache_dir=d, fetch=fetcher({})), [])
            os.utime(os.path.join(d, "taiwan-2028.json.missing"), (0, 0))
            self.assertEqual(len(S.taiwan_calendar([2028], cache_dir=d, fetch=fetcher({2028: YEAR_2026}))), 3)
            self.assertFalse(os.path.exists(os.path.join(d, "taiwan-2028.json.missing")))

    def test_default_years_this_and_next(self):
        calls = []
        entries, _ = S.load_calendar({"sources": [{"provider": "taiwan", "cache_dir": None}]},
                                     fetch=fetcher({}, calls), now=NOW)
        self.assertEqual([c.rsplit("/", 1)[1] for c in calls], ["2026.json", "2027.json"])
        self.assertEqual(entries, [])

    def test_ttl_parse(self):
        self.assertEqual([S._ttl_seconds(x) for x in ("24h", "30m", "1d", "90s", 0, 5)], [86400, 1800, 86400, 90, 0, 5])


class LoadCalendar(unittest.TestCase):
    def write(self, d, name, data):
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return p

    def test_merge_inline_file_http_taiwan(self):
        with tempfile.TemporaryDirectory() as d:
            f = self.write(d, "c.json", {"calendar": [{"date": "2099-01-01", "name": "假"}]})
            cfg = {"timezone": "Asia/Taipei", "calendar": [{"date": "2026-11-11", "name": "活動"}],
                   "sources": [{"provider": "file", "path": f}, {"provider": "http", "url": "http://x/c.json", "cache_dir": None},
                               {"provider": "taiwan", "years": [2026], "cache_dir": None}]}
            http = {"http://x/c.json": [{"date": "2030-01-01", "name": "http"}]}   # 直接回傳陣列也可以

            def fetch(url, headers=None, timeout=20):
                return http[url] if url in http else YEAR_2026
            entries, tz = S.load_calendar(cfg, fetch=fetch, now=NOW)
            self.assertEqual(tz, "Asia/Taipei")
            self.assertEqual(sorted(e["name"] for e in entries), sorted(["活動", "假", "http", "開國紀念日", "農曆除夕", "補行上班"]))

    def test_failing_source_does_not_break_others(self):
        with tempfile.TemporaryDirectory() as d:
            f = self.write(d, "c.json", {"calendar": [{"date": "2099-01-01", "name": "假"}]})
            cfg = {"sources": [{"provider": "file", "path": os.path.join(d, "nope.json")},
                               {"provider": "bogus"}, {"provider": "file", "path": f}]}
            entries, _ = S.load_calendar(cfg)
            self.assertEqual([e["name"] for e in entries], ["假"])

    def test_source_scope_applies_to_each_entry(self):
        # 台灣行事曆的每一天沒有 match：來源寫 match 才不會把 24×7 的服務一起吞掉（2026-10-adjust-schedule-verify.md）
        cfg = {"sources": [{"provider": "taiwan", "years": [2026], "cache_dir": None,
                            "match": {"service": "^office$"}, "series": "http\\..*"}],
               "calendar": [{"date": "2026-11-01", "name": "UPS", "type": "maintenance"}]}
        entries, _ = S.load_calendar(cfg, fetch=lambda url, headers=None, timeout=20: YEAR_2026, now=NOW)
        tw = [e for e in entries if e["name"] != "UPS"]
        self.assertTrue(tw and all(e["match"] == {"service": "^office$"} and e["series"] == "http\\..*" for e in tw))
        self.assertNotIn("match", next(e for e in entries if e["name"] == "UPS"))   # 直接寫的不受影響
        adjs = A.calendar_adjustments(entries, "Asia/Taipei")
        key = {"service": "fab"}
        self.assertFalse(any(A.match_keys(key, a["match"]) for a in adjs if a.get("match")))
        # 那一筆自己寫的優先
        entries, _ = S.load_calendar({"sources": [{"provider": "file", "path": self.write_tmp(), "detect": True}]})
        self.assertEqual([e["detect"] for e in entries], [True, False])

    def write_tmp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        return self.write(d, "c.json", {"calendar": [{"date": "2099-01-01", "name": "a"},
                                                     {"date": "2099-01-02", "name": "b", "detect": False}]})

    def test_disabled_source_skipped(self):
        calls = []
        S.load_calendar({"sources": [{"provider": "taiwan", "enabled": False}]}, fetch=fetcher({}, calls), now=NOW)
        self.assertEqual(calls, [])

    def test_adjust_load_reads_calendar_file(self):
        # adjust.load 走 sources：行事曆來源的項目變成 exclude_window
        with tempfile.TemporaryDirectory() as d:
            f = self.write(d, "c.json", {"calendar": [{"date": "2099-01-01", "name": "假"}]})
            cal = os.path.join(d, "calendar.yaml")
            with open(cal, "w", encoding="utf-8") as fh:
                fh.write(f"timezone: Asia/Taipei\nsources:\n  - {{provider: file, path: {f}}}\n")

            class NoES:
                def req(self, *a, **k):
                    raise RuntimeError("no es")
            got = A.load(NoES(), "aiops-tuning", {"calendar_file": cal, "adjustments_file": None})
            self.assertEqual([(a["kind"], a["source"]) for a in got], [("exclude_window", "calendar")])


class HttpProviderCache(unittest.TestCase):
    def test_http_source_is_cached(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            cfg = {"sources": [{"provider": "http", "url": "http://x/c.json", "cache_dir": d}]}

            def fetch(url, headers=None, timeout=20):
                calls.append(url)
                return {"calendar": [{"date": "2099-01-01", "name": "x"}]}
            for _ in range(3):
                entries, _tz = S.load_calendar(cfg, fetch=fetch)
            self.assertEqual((len(calls), len(entries)), (1, 1))


class LoadOwners(unittest.TestCase):
    def test_inline_first_then_sources(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "o.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"owners": [{"match": {"service": "^mes-"}, "owner": "檔案"}]}, f)
            cfg = {"owners": [{"match": {"service": "^mes-api$"}, "owner": "直接寫"}],
                   "owners_source": [{"provider": "file", "path": p}, {"provider": "http", "url": "http://x/o", "cache_dir": None}]}
            got = S.load_owners(cfg, fetch=lambda url, headers=None, timeout=20: [{"match": {}, "owner": "http"}])
            self.assertEqual([o["owner"] for o in got], ["直接寫", "檔案", "http"])

    def test_single_source_dict_and_failure(self):
        got = S.load_owners({"owners_source": {"provider": "file", "path": "/no/such/file.json"}})
        self.assertEqual(got, [])

    def test_fake_data_files_follow_the_interface(self):
        here = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(here, "fake-data", "owners.json"), encoding="utf-8") as f:
            owners = json.load(f)["owners"]
        from plugins.observability.chronic.chronic import owner_of
        self.assertEqual(owner_of({"resource.attributes.service.name": "mes-api"}, owners), "【假】MES 團隊")
        self.assertIsNone(owner_of({"service": "other"}, owners))
        with open(os.path.join(here, "fake-data", "calendar.json"), encoding="utf-8") as f:
            cal = json.load(f)
        adjs = A.calendar_adjustments(cal["calendar"], cal["timezone"])
        self.assertEqual(len(adjs), 3)
        self.assertTrue(all(pd.Timestamp(a["start"]).year >= 2098 for a in adjs))   # 假日期都在遙遠的未來，不會影響真的訓練（台北 2099-01-01 是 UTC 2098-12-31）


if __name__ == "__main__":
    unittest.main()

"""common/clock：時間只在邊界解析一次——字串進、帶時區的 Timestamp／Timedelta 出；往下的程式不收字串。"""
import unittest
from datetime import timedelta

import pandas as pd
import typer

from common.clock import TimeWindow, check_zone, now_of, pandas_freq, parse_duration, parse_instant, show_duration
from common.config import ConfigError, validate
from interface.cli.common import _duration, _instant


class Parse(unittest.TestCase):
    def test_duration_units(self):
        for text, want in {"30s": 30, "5m": 300, "30min": 1800, "1h": 3600, "7d": 604800, "2w": 1209600, "1.5h": 5400, "500ms": 0.5, "1D": 86400, "10Min": 600}.items():
            self.assertEqual(parse_duration(text), timedelta(seconds=want), text)
        self.assertEqual(parse_duration(0), timedelta(0))                     # rules 的 for 可以寫數字 0
        self.assertEqual(parse_duration(timedelta(minutes=1)), timedelta(minutes=1))

    def test_duration_garbage(self):
        for bad in ("", "abc", "10", "5x", "-5m", "m"):
            with self.assertRaises(ValueError, msg=bad):
                parse_duration(bad)

    def test_instant_is_always_tz_aware(self):
        self.assertEqual(parse_instant("2026-10-10T08:00:00Z"), pd.Timestamp("2026-10-10T08:00:00Z"))
        self.assertEqual(parse_instant("2026-10-10T08:00:00+08:00"), pd.Timestamp("2026-10-10T00:00:00Z"))
        naive = parse_instant("2026-10-10 08:00")
        self.assertIsNotNone(naive.tzinfo)                                     # 沒寫時區當 UTC，不留 naive
        self.assertEqual(naive, pd.Timestamp("2026-10-10T08:00:00Z"))
        with self.assertRaises(ValueError):
            parse_instant("昨天")

    def test_freq(self):
        self.assertEqual((pandas_freq("5m"), pandas_freq("1h"), pandas_freq("30s")), ("5min", "1h", "30s"))

    def test_show_duration(self):
        self.assertEqual([show_duration(pd.Timedelta(x)) for x in ("30min", "1h", "2D", "90s", "1W")], ["30m", "1h", "2d", "90s", "1w"])


class Boundaries(unittest.TestCase):
    def test_now_of_refuses_strings(self):
        self.assertEqual(now_of(pd.Timestamp("2026-10-10T08:00:00Z")), pd.Timestamp("2026-10-10T08:00:00Z"))
        self.assertIsNotNone(now_of(None).tzinfo)
        with self.assertRaises(TypeError):
            now_of("2026-10-10T08:00:00Z")

    def test_cli_parsers_turn_bad_input_into_a_usage_error(self):
        self.assertEqual(_instant("2026-10-10T08:00:00Z"), pd.Timestamp("2026-10-10T08:00:00Z"))
        self.assertEqual(_duration("30m"), pd.Timedelta("30min"))
        for parse in (_instant, _duration):
            with self.assertRaises(typer.BadParameter):
                parse("看不懂")

    def test_window(self):
        end = pd.Timestamp("2026-10-10T08:00:00Z")
        w = TimeWindow.ending(end, pd.Timedelta("30min"))
        self.assertEqual((w.start, w.end, w.length), (end - pd.Timedelta("30min"), end, pd.Timedelta("30min")))
        self.assertIn(end - pd.Timedelta("1min"), w)
        self.assertNotIn(end, w)                                               # [start, end)
        self.assertEqual(TimeWindow.ending(end, "1h").length, pd.Timedelta("1h"))
        with self.assertRaises(ValueError):
            TimeWindow(pd.Timestamp("2026-10-10 08:00"), end)                  # naive 不收
        with self.assertRaises(ValueError):
            TimeWindow(end, end - pd.Timedelta("1min"))


class Zones(unittest.TestCase):
    def test_check_zone(self):
        self.assertEqual(check_zone("Asia/Taipei"), "Asia/Taipei")
        with self.assertRaises(ValueError):
            check_zone("Mars/Olympus")

    def test_bad_zone_in_any_config_is_caught_at_load(self):
        for kind, raw in (("series", {"timezone": "Taipei"}), ("series", {"train": {"timezone": "Mars/X"}}), ("rca", {"indices": {"anomalies": "a", "traces": "t", "logs": "l"}, "timezone": "UTC+8"}),
                          ("chronic", {"timezone": "nope"}), ("schedule", {"timezone": "x"})):
            with self.subTest(kind=kind, raw=raw), self.assertRaises(ConfigError) as cm:
                validate(raw, kind)
            self.assertIn("不認得的時區", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

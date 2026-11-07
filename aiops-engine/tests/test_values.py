"""es._value_aggs／_bucket_value 的單元測試：直方圖的請求數、錯誤率，不需要 ES。"""
from tests.config_factory import series_of
import unittest

from data.store.series_repo import _bucket_value, _value_aggs

F = "metrics.http.server.request.duration"


class HistogramValueTest(unittest.TestCase):
    def test_hist_count_sums_counts(self):
        v = {"hist_count": F}
        self.assertEqual(_value_aggs(v), {"v": {"value_count": {"field": F}}})
        self.assertEqual(_bucket_value(v, {"doc_count": 20, "v": {"value": 1260}}), 1260)   # 次數，不是文件數

    def test_hist_ratio_is_count_ratio(self):
        v = {"hist_ratio": {"field": F, "query": {"range": {"attributes.http.response.status_code": {"gte": 500}}}}}
        aggs = _value_aggs(v)
        self.assertEqual(aggs["num"]["filter"], {"range": {"attributes.http.response.status_code": {"gte": 500}}})
        b = {"doc_count": 20, "v": {"value": 1260}, "num": {"doc_count": 10, "v": {"value": 60}}}
        self.assertAlmostEqual(_bucket_value(v, b), 60 / 1260)   # 依次數算，不是 10/20
        self.assertIsNone(_bucket_value(v, {"doc_count": 2, "v": {"value": 0}, "num": {"doc_count": 0, "v": {"value": 0}}}))


if __name__ == "__main__":
    unittest.main()


class HistRatioAbove(unittest.TestCase):
    """超過門檻的請求比例：percentile_ranks 給「≤ 門檻」的百分比，1 − 它；沒有請求是 None（不學、不判斷）。"""
    def test_aggs_and_value(self):
        v = {"hist_ratio_above": {"field": F, "gt": 2.0}}
        self.assertEqual(_value_aggs(v), {"v": {"percentile_ranks": {"field": F, "values": [2.0]}}, "n": {"value_count": {"field": F}}})
        self.assertAlmostEqual(_bucket_value(v, {"v": {"values": {"2.0": 97.5}}, "n": {"value_count": None, "value": 400}}), 0.025)
        self.assertEqual(_bucket_value(v, {"v": {"values": {"2.0": 100.0}}, "n": {"value": 400}}), 0.0)
        self.assertIsNone(_bucket_value(v, {"v": {"values": {"2.0": None}}, "n": {"value": 0}}))

    def test_is_a_rate_with_request_count_as_events(self):
        from detect.utils import value_kind
        from data.store.series_repo import _bucket_events
        spec = series_of({"name": "x", "value": {"hist_ratio_above": {"field": F, "gt": 2.0}}})
        self.assertEqual(value_kind(spec), "rate")
        self.assertEqual(_bucket_events(spec.value, {"n": {"value": 400}}), 400)

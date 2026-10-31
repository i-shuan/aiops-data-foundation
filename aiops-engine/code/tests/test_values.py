"""es._value_aggs／_bucket_value 的單元測試：直方圖的請求數、錯誤率，不需要 ES。"""
import unittest

from detector.es import _bucket_value, _value_aggs

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

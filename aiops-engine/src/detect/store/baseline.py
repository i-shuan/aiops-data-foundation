"""動態基準線（aiops-baseline）的索引與 mapping：一條序列、一個對象、每個星期幾 × 小時一筆（median、mad、p05、p95、n）。
學與比的計算在 detect/baseline.py；讀寫在 detect/store/baseline_repo.py。"""
from __future__ import annotations

from detect.store.anomaly import KEYS_MAPPING

BASELINE_INDEX = "aiops-baseline"
# baseline 讀回來的欄位。method 存成 baseline_method：序列的 keys 也可能有 method（HTTP method），會撞名
BASELINE_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution", "baseline_method", "baseline_source", "trained_at")
GROUP_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution")
RENAMED = {"baseline_method": "method"}
BASELINE_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "series": {"type": "keyword"}, "keys": KEYS_MAPPING, "dow": {"type": "byte"}, "hour": {"type": "byte"},
    "level": {"type": "keyword"}, "median": {"type": "double"}, "mad": {"type": "double"},
    "p05": {"type": "double"}, "p95": {"type": "double"}, "n": {"type": "integer"},
    "trained_at": {"type": "date"}, "history_from": {"type": "date"}, "history_to": {"type": "date"},
    "resolution": {"type": "keyword"}, "method": {"type": "keyword"}, "baseline_source": {"type": "keyword"},
    "events": {"type": "double"},
}}}

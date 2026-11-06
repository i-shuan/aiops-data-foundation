"""baseline 的序列查詢與 aiops-baseline 的 mapping。連線見 core.es；aiops-anomalies 的契約在 core/anomaly.py。
查詢本身在 sources/es_source.py（ESSourceBackend.series、alive_times；value 的寫法 → ES aggregation 也在那裡）、
sources/stores.py（ESStore.load_baseline…）；這裡的 fetch／alive_times／load_baseline／load_group_baselines 只是 as_backend(es).<方法>，名字留給指令與測試。"""
from __future__ import annotations

import pandas as pd

from core.anomaly import KEYS_MAPPING
from core.es import ES, doc_id  # noqa: F401  doc_id 給 cmd_*.py 用（從 .es 一起 import）
from sources.base import as_backend
from sources.shape import fill_zero as _fill_zero  # noqa: F401  補 0 的邏輯在 sources/shape.py（ES 與其他來源共用）；名字留給既有的 import
from sources.stores import BASELINE_COLUMNS, BASELINE_INDEX, _RENAMED  # noqa: F401  baseline 的 index 與讀回來的欄位，一份定義在 sources/stores.py

BASELINE_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "series": {"type": "keyword"}, "keys": KEYS_MAPPING, "dow": {"type": "byte"}, "hour": {"type": "byte"},
    "level": {"type": "keyword"}, "median": {"type": "double"}, "mad": {"type": "double"},
    "p05": {"type": "double"}, "p95": {"type": "double"}, "n": {"type": "integer"},
    "trained_at": {"type": "date"}, "history_from": {"type": "date"}, "history_to": {"type": "date"},
    "resolution": {"type": "keyword"}, "method": {"type": "keyword"}, "baseline_source": {"type": "keyword"},
    "events": {"type": "double"},
}}}
ANOMALY_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "series": {"type": "keyword"}, "keys": KEYS_MAPPING,
    "value": {"type": "double"}, "median": {"type": "double"}, "expected_low": {"type": "double"},
    "expected_high": {"type": "double"}, "z": {"type": "double"}, "level": {"type": "keyword"},
    "direction": {"type": "keyword"}, "detected_at": {"type": "date"},
    "resolution": {"type": "keyword"}, "method": {"type": "keyword"}, "baseline_source": {"type": "keyword"},
    "confidence": {"type": "keyword"}, "p": {"type": "double"},
    "status": {"type": "keyword"}, "streak": {"type": "integer"},
    "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "alert_id": {"type": "keyword"},   # 併進哪一件（investigator alert 寫回；儀表板依它撈這件的異常）
    # 哪一版 engine 產生的（core/version.py；owner.write_anomalies 補）。放在這裡而不是 RULE_FIELDS：index 是 dynamic false，
    # 但 ensure_index 對既有 index 也會 PUT _mapping 把這份 properties 補上，detect、rules、peer、quality、trace 每輪都呼叫，不用另外補
    "engine_version": {"type": "keyword"},
}}}


def value_kind(spec: dict) -> str:
    """count：值本身就是事件數（筆數、請求數）；rate：比例或百分位，事件數是底下的請求數；
    gauge：量測值（CPU、記憶體），不看事件數。預先統計好的筆數（例如 {sum: requests}）設 events: value。"""
    if spec.get("events") == "value":
        return "count"
    if isinstance(spec.get("events"), dict):   # 預先統計好的資料：事件數在另一個欄位（例如 {sum: requests}）
        return spec.get("kind", "rate")
    value = spec["value"]
    if value == "count":
        return "count"
    (kind, _), = value.items()
    return {"hist_count": "count", "ratio": "rate", "hist_ratio": "rate", "hist_ratio_above": "rate", "percentile": "rate",
            "div": "rate"}.get(kind, "gauge")


def fetch(es, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str | None) -> pd.DataFrame:
    """一條序列定義 → keys（沒有就 by）+ ts + value + n。es 是 ES 就在 ES 端 aggregation（sources/es_source.py），
    是檔案、HTTP、記憶體的 Backend 就在 pandas 整形（sources/shape.py），回傳格式一樣。"""
    return as_backend(es).series(spec, start, end, interval)


def alive_times(es, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str) -> pd.DatetimeIndex:
    """收集系統有在送資料的時間窗（Backend.alive_times）。"""
    return as_backend(es).alive_times(spec, start, end, interval)


def load_baseline(es, series: str, by: list[str]) -> pd.DataFrame:
    """這條序列自己學出來的 baseline（不含借給別人用的組 baseline）：欄位 by + BASELINE_COLUMNS。"""
    return as_backend(es).load_baseline(series, by)


def load_group_baselines(es, series: str) -> pd.DataFrame:
    """train 時從成員算出來的組 baseline：group + dow、hour、level、median、mad、resolution。"""
    return as_backend(es).load_group_baselines(series)

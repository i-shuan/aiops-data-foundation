"""baseline 的序列查詢。連線見 core.es。
查詢本身在 sources/es_source.py（ESSourceBackend.series、alive_times）、sources/stores.py（ESStore.load_baseline…）；
這裡的 fetch／alive_times／load_baseline／load_group_baselines 只是 as_backend(es).<方法>，名字留給指令與測試。
aiops-baseline、aiops-anomalies 的 mapping 在 sources/schema.py，value 的寫法 → ES aggregation 在 sources/es_aggs.py；這裡只 re-export。"""
from __future__ import annotations

import pandas as pd

from core.es import ES, doc_id  # noqa: F401  doc_id 給 cmd_*.py 用（從 .es 一起 import）
from sources import as_backend
from sources.es_aggs import _bucket_events, _bucket_value, _value_aggs, value_kind  # noqa: F401
from sources.schema import _RENAMED, ANOMALY_INDEX, ANOMALY_MAPPING, BASELINE_COLUMNS, BASELINE_INDEX, BASELINE_MAPPING  # noqa: F401
from sources.shape import fill_zero as _fill_zero  # noqa: F401  補 0 的邏輯在 sources/shape.py（ES 與其他來源共用）；名字留給既有的 import


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

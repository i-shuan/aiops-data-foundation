"""source／store／provider 的設定模型：每種設定檔最上層的 source、store 段（sources/__init__.py 的 open_backend 讀），
行事曆與負責人的外部來源（sources/providers.py）。"""
from __future__ import annotations

from typing import Any, Literal

from core.config import Dur, NonNeg, Strict


class Source(Strict):
    """資料從哪來（sources/）：沒寫＝ES。"""
    kind: Literal["es", "file", "http"] | None = None
    format: Literal["events", "series"] | None = None
    files: dict[str, str] | None = None
    spans: str | None = None
    url: str | None = None
    urls: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    timeout: NonNeg | None = None


class Store(Strict):
    """結果（baseline、異常、調整）放哪（sources/stores.py）。"""
    kind: Literal["memory", "file", "es"] | None = None
    dir: str | None = None



class Provider(Strict):
    """外部資料來源（sources/providers.py）：行事曆的 sources、慢性病的 owners_source。match／series／detect 只有行事曆用。"""
    provider: Literal["file", "http", "taiwan"]
    path: str | None = None
    url: str | None = None
    years: list[int] | None = None
    enabled: bool | None = None
    cache_dir: str | None = None
    cache_ttl: Dur | None = None
    headers: dict[str, str] | None = None
    timeout: NonNeg | None = None
    match: dict[str, Any] | None = None
    series: str | None = None
    detect: bool | None = None

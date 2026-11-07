"""共用的型別與基底（各功能的設定模型都用得到）；只屬於某個功能的小段在那個功能的 config/ 底下。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


# ── 共用型別 ──
Num = int | float


NonNeg = Annotated[int | float, Field(ge=0)]


Ratio = Annotated[int | float, Field(ge=0, le=1)]


PosInt = Annotated[int, Field(ge=1)]


# 時間長度：30s、1m、5m、15m、1h、14d、90s；pandas 的寫法（10min、2h30m 不收）
Dur = Annotated[str, StringConstraints(pattern=r"^\d+(\.\d+)?(ms|s|m|h|d|w)$")]


When = str | datetime | date                 # yaml 的 2026-10-10 解成 date、2026-10-10T02:00 解成 datetime、帶時區的是字串


Query = dict[str, Any] | list[dict[str, Any]]  # ES 的 query 片段；規則的 filter 可以是清單




Direction = Literal["up", "down", "both"]


Severity = Literal["warning", "high", "critical"]


Show = Literal["pct", "seconds", "hours", "ns_as_ms", "number", "value", "cores", "bytes"]


Op = Literal[">=", ">", "<=", "<", "==", "!="]


class Strict(BaseModel):
    """多出來的鍵就是錯（打錯字要在啟動時擋下）；欄位名跟 yaml 的鍵一樣，少數 Python 關鍵字用 alias（for、from）。
    設定是模型，用屬性讀（`series_config.interval`）；要分辨「yaml 有寫 null」跟「沒寫」用 `model_fields_set`。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _same_length(keys, by, what: str = "keys") -> None:
    if keys is not None and by is not None and len(keys) != len(by):
        raise ValueError(f"{what} 有 {len(keys)} 個、by 有 {len(by)} 個，要一樣多（{what} 是 by 的每個欄位各取一個名字）")

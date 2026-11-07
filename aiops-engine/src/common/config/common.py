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

    **過渡層（步驟 3 拆掉）**：設定原本是 dict，程式到處 `cfg.get("x", d)`、`cfg["x"]`。為了讓設定改成模型時現有的程式與測試
    不用一次全改，這裡讓模型也能這樣讀：語意跟原本的 dict 一樣——**只有 yaml 有寫的鍵才算有**（沒寫的 `get` 回 default、
    `in` 回 False）。每一種設定換成屬性存取（`series_config.interval`）之後，那一種就不再走這裡。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def _written(self) -> dict[str, str]:
        """yaml 有寫的鍵（用 yaml 的寫法，alias 優先）→ 欄位名。"""
        return {(f.alias or n): n for n, f in type(self).model_fields.items() if n in self.model_fields_set}

    def keys(self):
        return self._written().keys()

    def __getitem__(self, key):
        written = self._written()
        if key not in written:
            raise KeyError(key)
        return getattr(self, written[key])

    def get(self, key, default=None):
        written = self._written()
        return getattr(self, written[key]) if key in written else default

    def __contains__(self, key) -> bool:
        return key in self._written()

    def __len__(self) -> int:
        return len(self._written())

    def items(self):
        return [(k, getattr(self, n)) for k, n in self._written().items()]

    def values(self):
        return [getattr(self, n) for n in self._written().values()]

    def to_dict(self) -> dict:
        """原本 load_config 回的那種 dict（只有寫的鍵，巢狀也是 dict）。"""
        return self.model_dump(exclude_unset=True, by_alias=True)

    def __eq__(self, other):
        return self.to_dict() == other if isinstance(other, dict) else super().__eq__(other)

    __hash__ = None


def _same_length(keys, by, what: str = "keys") -> None:
    if keys is not None and by is not None and len(keys) != len(by):
        raise ValueError(f"{what} 有 {len(keys)} 個、by 有 {len(by)} 個，要一樣多（{what} 是 by 的每個欄位各取一個名字）")

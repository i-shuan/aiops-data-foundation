"""寫進 ES 的文件（契約）共同的基底：Incident、Fingerprint、Alert、Adjustment 都繼承它。
建構時多出來的鍵就是錯（打錯欄位名立刻擋）、賦值也驗；to_doc 只寫有給的欄位、不寫 None、時間欄位轉 ISO 字串；from_doc 讀舊文件寬鬆。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Doc(BaseModel):
    """寫進 ES 的文件共同的做法：建構時多出來的鍵就是錯（打錯欄位名立刻擋）；to_doc 不寫 None；from_doc 讀舊文件寬鬆。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)

    @classmethod
    def from_doc(cls, d: dict | None):
        """ES 的 _source → 物件。不認得的鍵忽略（舊文件、別的程式多寫的），缺的給預設值。"""
        d = d or {}
        names = {f.alias or n for n, f in cls.model_fields.items()} | set(cls.model_fields)
        return cls.model_validate({k: v for k, v in d.items() if k in names})

    def to_doc(self) -> dict:
        """寫進 ES 的 dict：欄位名用 ES 的（@timestamp）；只寫建構時有給的欄位（沒碰的預設值不寫，匯入的事故不會多出 anomaly_ids: []），
        給了但是 None 的也不寫；給了空陣列照寫。from_doc 讀進來再 to_doc 會跟原文件一樣。"""
        d = self.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}

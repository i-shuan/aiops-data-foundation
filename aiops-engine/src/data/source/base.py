"""一種原始資料格式（OTel、Elastic APM…）要提供什麼：② 清資料的「來源」介面。

engine 後面的部分（合成、判斷、告警、根因）只認統一的 span（見 data/store/models.py 的 Trace）與統一的異常；
一種來源怎麼把自己的 raw 文件轉成統一 span、查 raw trace 要撈哪些欄位、trace id 叫什麼，都在這一個類別裡。
新增一種來源＝寫一個子類別、登記到 data/source/__init__.py 的 FORMATS，不用改別的地方。
"""
from __future__ import annotations

from datetime import datetime


def duration_ms(v) -> float:
    """奈秒 → 毫秒。壞 SDK 送來的格式錯誤值（字串、負數）當 0，不能讓一筆壞 span 擋住整批合併或判斷。"""
    try:
        d = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return d / 1e6 if d > 0 else 0.0


def start_ms(ts) -> float:
    # ES 回的 @timestamp 可能是數字、數字的字串（奈秒精度時，例如 "1791687912264.916000"），或 ISO 格式
    if isinstance(ts, str) and ts.replace(".", "", 1).isdigit():
        ts = float(ts)
    return float(ts) if isinstance(ts, (int, float)) else datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000


class SourceFormat:
    """子類別覆寫下面這些；沒有預設的「通用」實作，免得新來源漏填也能跑。"""
    name: str = ""
    trace_fields: object = None          # 查 raw trace 的 `_source`（list 或 {includes, excludes}）
    trace_sort: list = []                # 同一個 trace 的 span 排序
    trace_id_field: str = ""             # raw 文件裡 trace id 的欄位名
    span_id_field: str = ""              # raw 文件裡 span id 的欄位名（用 span id 查回來時用）
    aliases: list[tuple[str, ...]] = []  # 調整的 match 可以互換的欄位名（短名 ↔ 這個來源的路徑）

    def span_ids_filter(self, span_ids: list[str]) -> dict:
        """查回這些 span id 的 ES query（用 span id 讀 span，例如 link 指到的別的 trace 裡的生產者）。"""
        raise NotImplementedError

    def is_doc(self, doc: dict) -> bool:
        """這筆 raw 文件是不是這個來源的格式（看內容，不看索引名）。"""
        raise NotImplementedError

    def owns_index(self, index: str) -> bool:
        """這個索引名是不是這個來源的（只用在「文件還沒讀回來、要先決定怎麼查」的時候）。"""
        raise NotImplementedError

    def to_span(self, doc: dict) -> dict:
        """raw 文件 → 統一的 span（dict：trace_id、span_id、parent、service、name、kind、target、start_ms、dur_ms、error、exception、links、attrs、resource）。"""
        raise NotImplementedError

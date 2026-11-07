"""來源適配器（SourceAdapter）：把一種原始資料的格式（OTel、Elastic APM…）翻成 engine 內部統一的格式。

這是 Adapter 模式：外面的系統各寫各的欄位、單位、結構；後面的合成、判斷、告警、根因只認統一的 span
（見 data/store/models.py 的 Trace）。一種來源一個適配器，要回答下面這些問題；新增一種來源＝寫一個子類別、登記到
data/source/__init__.py 的 ADAPTERS，不用改別的地方（怎麼新增：docs/data-sources-how-to.md；對照表：docs/data-sources-reference.md）。
"""
from __future__ import annotations

from common.infra.es import get_field

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


def with_paths(fields, paths: list[str]):
    """查 raw 的 `_source`（list 或 {includes, excludes}）多要幾個欄位（覆寫的部門、群組欄位不一定在預設的清單裡）。"""
    if not paths or fields is None:
        return fields
    if isinstance(fields, dict):
        inc = list(fields.get("includes") or [])
        return {**fields, "includes": inc + [p for p in paths if p not in inc]}
    return list(fields) + [p for p in paths if p not in fields]


def apply_owner(span: dict, doc: dict, owner: dict) -> dict:
    """span 的 resource.dept、resource.group 改用歸屬的欄位名從 raw 文件讀（含 /mapping 核准過的覆寫）。
    假設同一個來源的 agent 在 metrics 與 trace 用同一個路徑（APM 都是 labels.dept；OTel 都是 resource.attributes.dept）。
    歸屬沒寫那個欄位就不動；寫了但這筆文件沒有，就拿掉（跟沒設 label 一樣：group 退到 data_stream.namespace 或 unassigned）。"""
    keys = [k for k in ("dept", "group") if owner.get(k)]
    if not keys:
        return span
    res = dict(span.get("resource") or {})
    for k in keys:
        v = get_field(doc, owner[k])
        if v is None:
            res.pop(k, None)
        else:
            res[k] = v
    span["resource"] = res
    return span


class SourceAdapter:
    """子類別覆寫下面這些；沒有預設的「通用」實作，免得新來源漏填也能跑（漏填會丟 NotImplementedError）。"""
    name: str = ""
    trace_fields: object = None          # 查 raw trace 的 `_source`（list 或 {includes, excludes}）
    trace_sort: list = []                # 同一個 trace 的 span 排序
    trace_id_field: str = ""             # raw 文件裡 trace id 的欄位名
    span_id_field: str = ""              # raw 文件裡 span id 的欄位名（用 span id 查回來時用）
    aliases: list[tuple[str, ...]] = []  # 調整的 match 可以互換的欄位名（短名 ↔ 這個來源的路徑）

    def span_ids_filter(self, span_ids: list[str]) -> dict:
        """查回這些 span id 的 ES query（用 span id 讀 span，例如 link 指到的別的 trace 裡的生產者）。"""
        raise NotImplementedError

    def can_handle(self, doc: dict) -> bool:
        """這筆 raw 文件是不是這個來源的格式：看內容裡有沒有這個來源**才有的**欄位（不看索引名）。
        要積極地認：只在確定是自己的時候回 True，不要「不是別人的就是我的」——沒有任何適配器認得時，to_span 會丟 UnknownSourceError。"""
        raise NotImplementedError

    def owns_index(self, index: str) -> bool:
        """這個索引名是不是這個來源的（只用在「文件還沒讀回來、要先決定怎麼查」的時候）。"""
        raise NotImplementedError

    def to_span(self, doc: dict) -> dict:
        """raw 文件 → 統一的 span（dict：trace_id、span_id、parent、service、name、kind、target、start_ms、dur_ms、error、exception、links、attrs、resource）。"""
        raise NotImplementedError

"""資料來源介面：設定檔寫資料從哪來，指令和模型不用改（docs/pdca/meta/2026-11-model-modules.md）。

設定檔（series.yaml、peer.yaml、rules.yaml、trace.yaml…）最上層：

  source:                 # 沒寫＝ES（ESBackend：aggregation 在 ES 端算）
    kind: file            # file、http、es
    format: events        # events（一列一個事件）或 series（已整好：keys + ts + value [+ n]）
    files: {metrics-apm.transaction.1m-*: data/tx.csv}
  store:                  # 結果（baseline、異常、調整）放哪；source 不是 es 時預設 file（.aiops-store/）
    kind: file
    dir: .aiops-store

程式直接呼叫：sources.MemoryBackend({"metrics-*": df})，把它當 es 傳給 detector 的指令，或自己 .series(spec, …) 餵 models/。
手上是 core.es.ES（或假 ES）時 as_backend(es) 包成 ESBackend；detector/es.py 等讀寫函式開頭就是這麼做的。
索引名與 mapping 在 schema.py；span 的格式解析在 traces.py。
"""
from __future__ import annotations

from datetime import datetime

from .base import Backend, Store
from .es_source import ESBackend, ESSourceBackend
from .rows import FileBackend, HttpBackend, MemoryBackend, RowsBackend, load_file
from .shape import ShapeError, points_from_rows, series_from_rows
from .stores import ESStore, FileStore, MemoryStore, open_store

__all__ = ["Backend", "Store", "as_backend", "ESBackend", "ESSourceBackend", "FileBackend", "HttpBackend", "MemoryBackend", "RowsBackend",
           "load_file", "ShapeError", "points_from_rows", "series_from_rows", "ESStore", "FileStore", "MemoryStore", "open_store",
           "open_backend", "spans_of", "spans_by_id"]


def as_backend(x) -> Backend:
    """唯一的正規化點：已經是 Backend 就原樣回；core.es.ES 或長得像 ES 的東西（有 req／search 的假 ES）包成 ESBackend。
    放在這裡而不是 base.py：base 定義 Backend、es_source 繼承它，base 再 import ESBackend 就繞圈。"""
    if isinstance(x, Backend):
        return x
    if x is None or not any(hasattr(x, m) for m in ("req", "search", "bulk_index")):
        raise TypeError(f"要 sources.Backend 或 core.es.ES，不是 {type(x).__name__}")
    return ESBackend(x)


def open_backend(cfg: dict, es_factory) -> Backend:
    """指令開頭呼叫：依設定檔的 source／store 決定給哪個 Backend；沒寫 source 就是 ESBackend（讀寫都是 ES）。
    es_factory 是建 ES 連線的函式（指令傳自己模組裡的 ES，測試才蓋得掉），只在真的要連 ES 時才呼叫。"""
    src = dict(cfg.get("source") or {})
    st = dict(cfg.get("store") or {})
    kind = src.pop("kind", "es")
    if kind == "es" and st.get("kind", "es") == "es":
        return ESBackend(es_factory())
    store = open_store(st, es_factory, default="file" if kind != "es" else "memory")
    if kind == "es":
        return ESSourceBackend(es_factory(), store)
    fmt = src.pop("format", "events")
    if kind == "file":
        return FileBackend(src.get("files"), fmt, store, src.get("spans"))
    if kind == "http":
        return HttpBackend(src.get("url"), src.get("urls"), fmt, store, src.get("headers"), src.get("timeout", 60))
    raise ValueError(f"source.kind 只能是 es、file、http，不是 {kind!r}（程式裡直接給 DataFrame 用 sources.MemoryBackend）")


def spans_by_id(es, index: str, span_ids: list[str], start: datetime, end: datetime) -> dict[str, dict]:
    """用 span_id 讀回 span（例如 link 指到的、另一個 trace 裡的生產者）。ES 的查詢在 es_source.py；格式見 traces.py。"""
    return as_backend(es).spans_by_id(index, span_ids, start, end)


def spans_of(es, index: str, trace_ids: list[str], start: datetime, end: datetime) -> list[dict]:
    """把這些 trace 的所有 span 讀回來（含其他服務的）。時間放寬，跨過邊界的請求也讀得完整。
    ES 的查詢在 es_source.py；檔案來源的 span 已經是這個格式，直接拿。"""
    return as_backend(es).spans(index, trace_ids, start, end)

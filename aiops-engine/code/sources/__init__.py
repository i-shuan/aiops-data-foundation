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
"""
from __future__ import annotations

from .base import Backend, Store, as_backend
from .es_source import ESBackend, ESSourceBackend
from .rows import FileBackend, HttpBackend, MemoryBackend, RowsBackend, load_file
from .shape import ShapeError, points_from_rows, series_from_rows
from .stores import ESStore, FileStore, MemoryStore, open_store

__all__ = ["Backend", "Store", "as_backend", "ESBackend", "ESSourceBackend", "FileBackend", "HttpBackend", "MemoryBackend", "RowsBackend",
           "load_file", "ShapeError", "points_from_rows", "series_from_rows", "ESStore", "FileStore", "MemoryStore", "open_store",
           "open_backend"]


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

"""資料來源與結果存放的介面（docs/pdca/meta/2026-11-model-modules.md、2026-11-oop-core.md）。

指令（train、detect、peer、rules…）拿到的是一個 Backend：讀原始資料（Source 的五個方法）＋讀寫 aiops-*（Store）。
ES 也是其中一個實作（es_source.ESBackend ＝ ESSourceBackend 讀 ＋ stores.ESStore 寫）：設定沒寫 source 時 open_backend 給的就是它，
原本散在 detector/es.py、rules.py、owner.py、adjust.py、core/traces.py、investigator/similar.py 的 ES 查詢都搬成它的方法。
detector/es.py 那些讀寫函式還在（名字給指令用），但只剩一行：as_backend(es).<方法>(…)。
as_backend 是唯一的正規化點：傳進來的已經是 Backend 就原樣用，是 core.es.ES 或測試的假 ES（只有 req／search）就包成 ESBackend，
所以指令、測試裡的假 ES 都不用改，程式裡也不再有「是不是 Backend」的分支。

五種標準輸入（一列是什麼）：
  series     keys… + ts + value + n        一條序列在一個時間窗的值；n 是底下的事件數（M1 動態基準線、M3 的 window 型規則、M2 同儕比對用 interval=None）
  points     {key 元組: [(ts, doc), …]}    一個對象一個時間點的原始文件（M3 的 value／compare／ratio／increase／slope 型規則）
  spans      core/traces.py 的 span 格式    一列一段（M4 trace 分段）
  incidents  [(id, 文件), …]               候選事故，文件要有 fingerprint、status（M5 相似事故檢索）
  rows       [{欄位: 值}, …]               來源的原始列（ingest 讀 ML／FDC 判好的異常、工廠 paths-merge 讀 MES）
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.version import ENGINE_VERSION

import pandas as pd


class Store(ABC):
    """aiops-baseline、aiops-anomalies、aiops-tuning（以及 detect-status 寫的 aiops-alerts）的讀寫。"""

    kind: str = "?"

    @abstractmethod
    def ensure_index(self, name: str, body: dict) -> None: ...

    @abstractmethod
    def bulk_index(self, index: str, docs: list[tuple[str, dict]]) -> None: ...

    def req(self, method: str, path: str, body=None, ok=None):
        """其他零星的 ES 呼叫（補 mapping、刪文件）：不是 ES 的 Store 當作沒事。"""
        return {}

    @abstractmethod
    def load_baseline(self, series: str, by: list[str]) -> pd.DataFrame: ...

    @abstractmethod
    def load_group_baselines(self, series: str) -> pd.DataFrame: ...

    @abstractmethod
    def save_baseline(self, series: str, docs: list[tuple[str, dict]], trained_at: str, had_data: bool) -> None: ...

    @abstractmethod
    def anomaly_status(self, ids: list[str]) -> dict[str, dict]:
        """{_id: {status, streak}}，只回 status 是 anomaly 的（cmd_baseline._keep_upgraded）。"""

    @abstractmethod
    def adjustments(self) -> list[dict]:
        """aiops-tuning 的全部文件（帶 id），依 at 排序（detector/adjust.load）。讀不到時回 []（印一行），不讓 train／detect 失敗。"""

    @abstractmethod
    def write_anomalies(self, docs: list[tuple[str, dict]]) -> None:
        """寫進異常總表並補 dept、group（查得到歸屬的 Store 自己查；查不到的全部 unassigned）。engine_version 由 detector.owner 補。"""

    @abstractmethod
    def incidents(self, statuses: tuple[str, ...], exclude_id: str | None = None, having: tuple[str, ...] = ()) -> list[tuple[str, dict]]:
        """aiops-incidents 裡 status 在 statuses、having 的欄位都有值的（M5 的候選、留一法評測）。"""

    def similar_incidents(self, fp, statuses: tuple[str, ...], exclude_id: str | None = None, size: int = 200) -> list[tuple[str, dict]]:
        """跟指紋 fp（core.incident.Fingerprint 或同形狀的 dict）可能像的候選（分數一律由 investigator.similar.rank 自己算）。
        預設就是全部 statuses 的；ES 用指紋先篩、只要前 size 件。"""
        return self.incidents(statuses, exclude_id)


class Backend(ABC):
    """Source（原始資料）＋ Store（結果）。指令只拿到這一個物件。"""

    kind: str = "?"
    store: Store

    # ── Source：五種標準輸入 ──
    @abstractmethod
    def series(self, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str | None) -> pd.DataFrame:
        """一條序列定義（series.yaml 的一筆：index、by、filter、value、keys、scale、events、fill_zero）→ keys + ts + value + n。
        interval None：整段算一個值，ts 是 end（同儕比對、window 型規則）。"""

    @abstractmethod
    def alive_times(self, spec: dict, start: pd.Timestamp, end: pd.Timestamp, interval: str) -> pd.DatetimeIndex:
        """這條序列的 index 裡有任何資料的時間窗（不加序列自己的 filter）：收集系統有在跑的時段。"""

    @abstractmethod
    def points(self, rule: dict, start: pd.Timestamp, end: pd.Timestamp) -> dict[tuple, list[tuple[pd.Timestamp, dict]]]: ...

    @abstractmethod
    def spans(self, index: str, trace_ids: list[str], start, end) -> list[dict]: ...

    def spans_by_id(self, index: str, span_ids: list[str], start, end) -> dict[str, dict]:
        want = set(span_ids)
        return {s["span_id"]: s for s in self.spans(index, [], start, end) if s.get("span_id") in want}

    @abstractmethod
    def source_rows(self, inp: dict, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
        """inp（index、time_field、filter）在 [start, end) 的原始列（detector/cmd_ingest.read_rows）。"""

    # ── 只有 ES 來源有的：直接下查詢（工廠 paths-merge 的 terms 查、放 index template）。呼叫前看 kind == "es" ──
    def raw_search(self, index: str, body: dict) -> dict:
        raise NotImplementedError(f"{self.kind} 來源沒有 ES 可以直接查")

    def raw_req(self, method: str, path: str, body=None, ok=None):
        raise NotImplementedError(f"{self.kind} 來源沒有 ES 可以直接呼叫")

    # ── Store：轉交 ──
    def ensure_index(self, name: str, body: dict) -> None:
        self.store.ensure_index(name, body)

    def bulk_index(self, index: str, docs: list[tuple[str, dict]]) -> None:
        self.store.bulk_index(index, docs)

    def req(self, method: str, path: str, body=None, ok=None):
        return self.store.req(method, path, body, ok)

    def load_baseline(self, series: str, by: list[str]) -> pd.DataFrame:
        return self.store.load_baseline(series, by)

    def load_group_baselines(self, series: str) -> pd.DataFrame:
        return self.store.load_group_baselines(series)

    def save_baseline(self, series: str, docs, trained_at: str, had_data: bool) -> None:
        self.store.save_baseline(series, docs, trained_at, had_data)

    def anomaly_status(self, ids: list[str]) -> dict[str, dict]:
        return self.store.anomaly_status(ids)

    def adjustments(self) -> list[dict]:
        return self.store.adjustments()

    def write_anomalies(self, docs) -> None:
        """寫進異常總表。每筆帶 engine_version（core/version.py）：五種偵測都從這裡寫，「每筆異常都要有」的欄位只補在這一處；
        dept、group 由 Store 補（ES 查 core/owner.py；檔案、記憶體來源沒有歸屬可查，全部 unassigned）。"""
        for _, d in docs:
            d["engine_version"] = ENGINE_VERSION
        self.store.write_anomalies(docs)

    def incidents(self, statuses: tuple[str, ...], exclude_id: str | None = None, having: tuple[str, ...] = ()) -> list[tuple[str, dict]]:
        return self.store.incidents(statuses, exclude_id, having)

    def similar_incidents(self, fp, statuses: tuple[str, ...], exclude_id: str | None = None, size: int = 200) -> list[tuple[str, dict]]:
        return self.store.similar_incidents(fp, statuses, exclude_id, size)


def as_backend(x) -> Backend:
    """唯一的正規化點：已經是 Backend 就原樣回；core.es.ES 或長得像 ES 的東西（有 req／search 的假 ES）包成 ESBackend。"""
    if isinstance(x, Backend):
        return x
    if x is None or not any(hasattr(x, m) for m in ("req", "search", "bulk_index")):
        raise TypeError(f"要 sources.Backend 或 core.es.ES，不是 {type(x).__name__}")
    from .es_source import ESBackend   # rows／es_source import 這裡，所以在這裡才 import
    return ESBackend(x)


def write_anomalies(es, docs: list) -> None:
    """偵測指令寫異常總表的入口：es 是 Backend 或 core.es.ES。docs：(_id, 文件)。空的不碰 ES。"""
    if not docs:
        return
    as_backend(es).write_anomalies(docs)

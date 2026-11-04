"""資料來源與結果存放的介面（docs/pdca/meta/2026-11-model-modules.md）。

指令（train、detect、peer、rules…）拿到的是一個 Backend：讀原始資料（Source 的五個方法）＋讀寫 aiops-*（Store）。
ES 本身不是 Backend：設定沒寫 source 時指令拿到的還是 core.es.ES，程式走原本的路（aggregation 在 ES 端算）。
detector/es.py、rules.py、owner.py、adjust.py、core/traces.py、investigator/similar.py 的讀寫函式開頭都看一眼
「傳進來的是不是 Backend」，是就交給它，不是就照舊打 ES——所以指令、測試裡的假 ES 都不用改。

五種標準輸入（一列是什麼）：
  series     keys… + ts + value + n        一條序列在一個時間窗的值；n 是底下的事件數（M1 動態基準線、M3 的 window 型規則、M2 同儕比對用 interval=None）
  points     {key 元組: [(ts, doc), …]}    一個對象一個時間點的原始文件（M3 的 value／compare／ratio／increase／slope 型規則）
  spans      core/traces.py 的 span 格式    一列一段（M4 trace 分段）
  incidents  [(id, 文件), …]               候選事故，文件要有 fingerprint、status（M5 相似事故檢索）
"""
from __future__ import annotations

from abc import ABC, abstractmethod

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
        """aiops-tuning 的全部文件（帶 id），依 at 排序（detector/adjust.load）。"""

    @abstractmethod
    def write_anomalies(self, docs: list[tuple[str, dict]]) -> None: ...

    @abstractmethod
    def incidents(self, statuses: tuple[str, ...], exclude_id: str | None = None) -> list[tuple[str, dict]]:
        """aiops-incidents 裡 status 在 statuses 的（M5 的候選）。"""


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
        self.store.write_anomalies(docs)

    def incidents(self, statuses: tuple[str, ...], exclude_id: str | None = None) -> list[tuple[str, dict]]:
        return self.store.incidents(statuses, exclude_id)


def is_backend(x) -> bool:
    return isinstance(x, Backend)

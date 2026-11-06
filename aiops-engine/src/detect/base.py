"""偵測指令的骨架（Detector）；它產生的 Anomaly 定義在 detect/store/models.py。

rules、peer、quality 的流程一樣：連線 → 補 index/mapping → 算這一輪的時間 → `--only` 過濾 → 每個檢查算出異常、印一行
→ 一次寫進異常總表 → 印總結。差別只在「檢查從哪來」「怎麼算」「時間格怎麼取」，各自覆寫那幾個 hook。
trace 的三種偵測與 baseline 的 save_anomalies 流程不同，只用 Anomaly.to_doc()。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from shared.infra.es import ES
from detect.store.models import Anomaly
from shared.infra.repos import Repos
from shared.scenario import load_scenario
from detect.utils import _freq, _now


class Detector(ABC):
    """偵測指令的骨架（template method）。子類別給 level、checks()、evaluate()，其餘有預設。

    run()：connect → prepare → now／slot → 每個 selected 的 check：evaluate → 印 [severity] message、收 (doc_id, doc)
          → after_check → 全部一次 write → 印 summary → exit_code。
    """

    level: str                       # 寫進異常的 level
    interval_default = "1m"          # stamp() 取整的粒度：cfg.interval 沒寫時用這個

    def __init__(self, cfg: dict, args, repo: Repos | None = None):
        self.cfg, self.args, self.repo = cfg, args, repo
        self.names = set(args.only.split(",")) if getattr(args, "only", None) else None
        self.now = self.slot = None
        self.docs: list[tuple[str, dict]] = []
        self.checked = self.fired = 0
        self.failed: list[str] = []

    # ── hooks ──
    def connect(self) -> Repos:
        """程式直接給 repo（測試的記憶體版）時用它，否則連 ES。"""
        return self.repo or Repos(ES(), load_scenario())

    def prepare(self) -> None:
        self.repo.anomalies.ensure()   # 舊的 index 補上規則用的欄位

    def stamp(self) -> pd.Timestamp:
        """這一輪異常的 @timestamp：now 取整到 interval。"""
        return self.now.floor(_freq(self.cfg.get("interval", self.interval_default)))

    @abstractmethod
    def checks(self) -> list[dict]:
        """要跑的檢查（prepare、now、slot 都有了才呼叫，要先載入、先驗的也在這裡）。"""

    def selected(self, check: dict) -> bool:
        return self.names is None or check["name"] in self.names

    @abstractmethod
    def evaluate(self, check: dict) -> list[Anomaly]: ...

    def on_error(self, check: dict, exc: Exception) -> None:
        """一個檢查壞了怎麼辦：預設整輪失敗（rules 覆寫成記下來、跳過那一條）。"""
        raise exc

    def after_check(self, check: dict, hits: list[Anomaly]) -> None:
        """一個檢查跑完（異常都印了）之後要印的（peer 的「幾組、幾個 Pod 比過」）。"""

    def doc_id(self, a: Anomaly) -> str:
        return a.id()

    def write(self, docs: list[tuple[str, dict]]) -> None:
        self.repo.anomalies.write(docs)

    def summary(self) -> str:
        return f"{self.slot.tz_convert('Asia/Taipei'):%m-%d %H:%M}：{self.checked} 個檢查，觸發 {self.fired} 個"

    def exit_code(self) -> int:
        return 1 if self.failed else 0

    # ── template ──
    def run(self) -> int:
        self.repo = self.connect()
        self.prepare()
        self.now = _now(self.args.at)
        self.slot = self.stamp()
        for check in self.checks():
            if not self.selected(check):
                continue
            self.checked += 1
            try:
                hits = self.evaluate(check)
            except Exception as e:
                self.on_error(check, e)
                continue
            for a in hits:
                print(f"  [{a.severity}] {a.message}")
                self.docs.append((self.doc_id(a), a.to_doc()))
            self.fired += len(hits)
            self.after_check(check, hits)
        if self.docs:
            self.write(self.docs)
        print(self.summary())
        return self.exit_code()

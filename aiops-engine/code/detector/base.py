"""異常文件（Anomaly）與偵測指令的骨架（Detector）。

rules、peer、quality 的流程一樣：開 backend → 補 index/mapping → 算這一輪的時間 → `--only` 過濾 → 每個檢查算出異常、印一行
→ 一次 write_anomalies → 印總結。差別只在「檢查從哪來」「怎麼算」「時間格怎麼取」，各自覆寫那幾個 hook。
trace 的三種偵測與 baseline 的 save_anomalies 流程不同，只用 Anomaly.to_doc()。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from core.es import doc_id
from .es import ANOMALY_INDEX, ANOMALY_MAPPING, ES
from .common import RULE_FIELDS, _freq, _now
from .owner import write_anomalies
from sources import open_backend


@dataclass
class Anomaly:
    """寫進 aiops-anomalies 的一筆異常。**這是異常文件唯一的定義處**：sources/schema.py 的 ANOMALY_MAPPING、
    RULE_FIELDS 要跟它一致（tests/test_detector_base.py 會對）。to_doc() 產生寫入的 dict。

    欄位            誰寫                      意思
    ts→@timestamp   全部                      異常落在哪個時間格：rules／quality 是 now 取整到 interval、peer 到分、baseline／trace 是資料點的時間
    series          全部                      哪條序列或規則（rules 是 rule.<名>、trace 的規則是 trace.segment.<名>）
    keys            全部                      對象（服務、Pod…）；rules 多帶 context 欄位、quality 缺欄位時多一個 field
    value           全部                      這次的值
    level           全部                      rule／peer／quality；baseline 是 dow_hour／hour／all／group
    median          全部（沒有就 None）       平常的值；quality 放昨天的值
    expected_low/   全部（沒有就 None）       正常範圍；rules 用 expected_high 放門檻
      expected_high
    z               全部（沒有就 None）       偏離幾倍
    direction       全部                      up／down／both；rules 放 op（>=、<=）
    detected_at     全部                      寫入的時間（沒給就在 to_doc 時補）
    rule            rules、peer、quality、trace 規則   規則名（RULE_FIELDS 的欄位；peer、quality 放檢查名）
    severity        同上                      warning／critical
    message         同上                      給人看的一句話（Grafana 直接顯示）
    since           rules                     從什麼時候開始超過門檻（只在 _source，沒進 mapping）
    resolution      baseline                  粒度（30s…1h；粗粒度沒有時是 None，但鍵一定在）
    method          baseline                  mad／poisson
    baseline_source baseline                  self 或 group:<組名>
    confidence      baseline                  high／medium／low
    status          baseline                  anomaly／warning（連續幾點才算；cmd_baseline.with_status）
    streak、p       baseline（有才寫）        連續第幾個、卜瓦松的機率
    不在這裡：dept、group、engine_version 由 owner.write_anomalies 補；alert_id 由 investigator 併件時寫回。
    """
    ts: pd.Timestamp
    series: str
    keys: dict
    value: float
    level: str
    median: float | None = None
    expected_low: float | None = None
    expected_high: float | None = None
    z: float | None = None
    direction: str | None = None
    detected_at: str | None = None
    rule: str | None = None
    severity: str | None = None
    message: str | None = None
    since: pd.Timestamp | None = None
    resolution: str | None = None
    method: str | None = None
    baseline_source: str | None = None
    confidence: str | None = None
    status: str | None = None
    streak: int | None = None
    p: float | None = None

    # 一定寫（沒有就 None）；其餘有值才寫。resolution 跟著 method 一起寫：baseline 的異常粗粒度時 resolution 是 None、鍵仍要在
    ALWAYS = ("median", "expected_low", "expected_high", "z", "level", "direction")

    def id(self) -> str:
        """預設的 _id：同一條序列、同一個對象、同一個時間格只有一筆（重跑會覆寫）。"""
        return doc_id(self.series, self.keys, self.ts)

    def to_doc(self, detected_at: str | None = None) -> dict:
        d = {"@timestamp": self.ts.isoformat(), "series": self.series}
        if self.rule is not None:
            d["rule"] = self.rule
        d.update({"keys": self.keys, "value": self.value})
        d.update({f: getattr(self, f) for f in self.ALWAYS})
        for f in ("severity", "message"):
            if getattr(self, f) is not None:
                d[f] = getattr(self, f)
        if self.since is not None:
            d["since"] = self.since.isoformat()
        d["detected_at"] = self.detected_at or detected_at or pd.Timestamp.now(tz="UTC").isoformat()
        if self.method is not None:
            d["resolution"] = self.resolution
        for f in ("method", "baseline_source", "confidence", "status", "streak", "p"):
            if getattr(self, f) is not None:
                d[f] = getattr(self, f)
        return d


class Detector(ABC):
    """偵測指令的骨架（template method）。子類別給 level、checks()、evaluate()，其餘有預設。

    run()：connect → prepare → now／slot → 每個 selected 的 check：evaluate → 印 [severity] message、收 (doc_id, doc)
          → after_check → 全部一次 write → 印 summary → exit_code。
    """

    level: str                       # 寫進異常的 level
    index, mapping = ANOMALY_INDEX, ANOMALY_MAPPING
    interval_default = "1m"          # stamp() 取整的粒度：cfg.interval 沒寫時用這個

    def __init__(self, cfg: dict, args, backend=None):
        self.cfg, self.args, self.backend = cfg, args, backend
        self.names = set(args.only.split(",")) if getattr(args, "only", None) else None
        self.es = None
        self.now = self.slot = None
        self.docs: list[tuple[str, dict]] = []
        self.checked = self.fired = 0
        self.failed: list[str] = []

    # ── hooks ──
    def connect(self):
        """設定檔沒寫 source 就是 ES；程式直接給 backend 時用它。"""
        return self.backend or open_backend(self.cfg, ES)

    def prepare(self) -> None:
        self.es.ensure_index(self.index, self.mapping)
        self.es.req("PUT", f"/{self.index}/_mapping", {"properties": RULE_FIELDS})   # 舊的 index 補上規則用的欄位

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
        write_anomalies(self.es, docs)

    def summary(self) -> str:
        return f"{self.slot.tz_convert('Asia/Taipei'):%m-%d %H:%M}：{self.checked} 個檢查，觸發 {self.fired} 個"

    def exit_code(self) -> int:
        return 1 if self.failed else 0

    # ── template ──
    def run(self) -> int:
        self.es = self.connect()
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

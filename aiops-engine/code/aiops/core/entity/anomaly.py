"""④⑤ aiops-anomalies 的契約：index 名、mapping、Anomaly（寫進去的一筆）。
誰寫：detector/（baseline、rules、peer、quality、trace、ingest，都經 core/repository/anomaly.py 的 AnomalyRepository.write）；誰讀：investigator/（併件）、feedback/（回饋、回測、量化）。
寫的人和讀的人在不同階段，所以放 core/（docs/pdca/meta/2026-11-clean-code.md）。"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from aiops.core.connection import doc_id

ANOMALY_INDEX = "aiops-anomalies"
KEYS_MAPPING = {"type": "flattened"}     # 序列的 keys（對象）：欄位隨序列設定而變，用 flattened
ANOMALY_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "series": {"type": "keyword"}, "keys": KEYS_MAPPING,
    "value": {"type": "double"}, "median": {"type": "double"}, "expected_low": {"type": "double"},
    "expected_high": {"type": "double"}, "z": {"type": "double"}, "level": {"type": "keyword"},
    "direction": {"type": "keyword"}, "detected_at": {"type": "date"},
    "resolution": {"type": "keyword"}, "method": {"type": "keyword"}, "baseline_source": {"type": "keyword"},
    "confidence": {"type": "keyword"}, "p": {"type": "double"},
    "status": {"type": "keyword"}, "streak": {"type": "integer"},
    "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "alert_id": {"type": "keyword"},   # 併進哪一件（investigator alert 寫回；儀表板依它撈這件的異常）
    # 哪一版 engine 產生的（aiops.ENGINE_VERSION；AnomalyRepository.write 補）。放在這裡而不是 RULE_FIELDS：index 是 dynamic false，
    # 但 ensure_index 對既有 index 也會 PUT _mapping 把這份 properties 補上，detect、rules、peer、quality、trace 每輪都呼叫，不用另外補
    "engine_version": {"type": "keyword"},
}}}
# 規則、同儕、品質、trace 的偵測多寫的三個欄位（既有索引由 AnomalyRepository.ensure 補上）
RULE_FIELDS = {"severity": {"type": "keyword"}, "message": {"type": "text"}, "rule": {"type": "keyword"}}


@dataclass
class Anomaly:
    """寫進 aiops-anomalies 的一筆異常。**這是異常文件唯一的定義處**：同一檔的 ANOMALY_MAPPING、detector/common.py 的
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
    不在這裡：dept、group、engine_version 由 AnomalyRepository.write 補（core/repository/owner.py 查歸屬）；alert_id 由 investigator 併件時寫回。
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

"""detect 的資料物件與 mapping：

- anomaly：④⑤ aiops-anomalies 的契約：index 名、mapping、Anomaly（寫進去的一筆）。
- baseline：動態基準線（aiops-baseline）的索引與 mapping：一條序列、一個對象、每個星期幾 × 小時一筆（median、mad、p05、p95、n）。
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from data.store.owner_repo import NODE_KEYS, NS_KEYS     # namespace、節點的欄位名（歸屬與歸件共用，定義在清資料那邊）
from common.infra.es import doc_id, get_field


# ── anomaly ──
ANOMALY_TABLE = "aiops_anomalies"         # PostgreSQL 的表（common/infra/pg.py）


@dataclass
class Anomaly:
    """寫進 aiops_anomalies（PostgreSQL）的一筆異常。**這是異常文件唯一的定義處**。to_doc() 產生寫入的 dict。

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
    rule            rules、peer、quality、trace 規則   規則名（peer、quality 放檢查名）
    severity        同上                      warning／critical
    message         同上                      給人看的一句話（Grafana 直接顯示）
    since           rules                     從什麼時候開始超過門檻（只在 _source，沒進 mapping）
    resolution      baseline                  粒度（30s…1h；粗粒度沒有時是 None，但鍵一定在）
    method          baseline                  mad／poisson
    baseline_source baseline                  self 或 group:<組名>
    confidence      baseline                  high／medium／low
    status          baseline                  anomaly／warning（連續幾點才算；cmd_baseline.with_status）
    streak、p       baseline（有才寫）        連續第幾個、卜瓦松的機率
    不在這裡：dept、group、engine_version 由 AnomalyRepository.write 補（detect/store/owner_repo.py 查歸屬）；alert_id 由 investigator 併件時寫回。
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


# ── 一筆異常歸到哪一件：keys 裡哪個欄位是「對象」由場景給（Scenario.entity_keys）──

def entity_of(a: dict, entity_keys: tuple[str, ...]) -> tuple[str, str]:
    """(類別, 名稱)：這筆異常歸到哪一件。對象（服務／機台）優先；沒有的用 namespace、再用節點；資料品質另外一類（送平台）。"""
    keys = a.get("keys") or {}
    cat = "data_quality" if a.get("level") == "quality" else "service"
    for kind, fields in (("service", entity_keys), ("namespace", NS_KEYS), ("node", NODE_KEYS)):
        for f in fields:
            if keys.get(f):
                return (cat if kind == "service" or cat == "data_quality" else kind), str(keys[f])
    if cat == "data_quality" and keys:
        return cat, "、".join(str(v) for v in keys.values())
    return cat if cat == "data_quality" else "other", a.get("series", "?")


def entity_value(keys: dict | None, entity_keys: tuple[str, ...]) -> str | None:
    """keys 裡的對象名稱（服務／機台）；沒有回 None。"""
    for k in entity_keys:
        if (keys or {}).get(k):
            return str(keys[k])
    return None


# ── baseline ──
BASELINE_TABLE = "aiops_baseline"
# baseline 讀回來的欄位。method 存成 baseline_method：序列的 keys 也可能有 method（HTTP method），會撞名
BASELINE_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution", "baseline_method", "baseline_source", "trained_at")
GROUP_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution")
RENAMED = {"baseline_method": "method"}

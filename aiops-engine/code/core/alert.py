"""⑥⑧ aiops-alerts 的契約：index 名、mapping、@timestamp 往前寫的規則（stamp_of）、Alert（一件告警，寫進去的一筆）。
誰寫：investigator/cmd_alert（一件一筆、心跳）、detector/cmd_baseline（detect-status）、api/（標 rca_requested）；
誰讀：investigator/（下一輪併件）、api/（表單、清單）、feedback/（量化）、Grafana。寫的人和讀的人在不同階段，所以放 core/。

Alert 是 ⑥ 併件時在手上的那個物件（investigator/alerts.py 只動它的屬性），同時是寫進 ES 的格式（to_doc）、讀回來的格式（from_doc）。
時間欄位在物件裡是 datetime（ES 的字串、"Z" 結尾都收），to_doc 轉 ISO 字串。這一輪算的、不寫 ES 的（anoms、new…）標 exclude。
心跳與 detect-status 不是一件事，照舊是兩個小 dict（heartbeat_doc、write_detect_status）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar, Literal

from pydantic import Field, field_validator

from .doc import Doc

ALERT_INDEX = "aiops-alerts"
# 開著的件、心跳的 @timestamp 往前寫這麼多（docs/pdca/phase3-locate/2026-10-alert-flap.md）。
# 一件只有一筆、每輪覆蓋；Grafana 的查詢範圍是「排定的評估時間往前 N 分鐘」，查詢卻晚幾秒才送出。
# 覆蓋剛好落在這幾秒之間時，@timestamp 比範圍的結束點還晚，Grafana 查到 0 筆、當成恢復（實測）。
# 往前寫 1 分鐘：主機間時鐘差、Grafana 的延遲都遠小於它；規則的窗是 5 分鐘（心跳 10 分鐘），還剩 4 分鐘的餘裕。
STAMP_LAG = timedelta(minutes=1)


def stamp_of(now: datetime) -> datetime:
    """開著的件、心跳寫進 aiops-alerts 的 @timestamp（Grafana 用它判斷「最近還有沒有」）。"""
    return now - STAMP_LAG


ALERT_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "alert_id": {"type": "keyword"}, "category": {"type": "keyword"},
    "service": {"type": "keyword"}, "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "severity": {"type": "keyword"}, "status": {"type": "keyword"},
    "started_at": {"type": "date"}, "updated_at": {"type": "date"}, "resolved_at": {"type": "date"},
    "anomaly_ids": {"type": "keyword"}, "anomaly_count": {"type": "integer"}, "series": {"type": "keyword"},
    "routes": {"type": "keyword"},
    # 跨服務（一層）：這件連帶了哪些上游服務；上游那件被整件併進下游時，上游那件標 resolved、merged_into 指到下游那件
    "upstream_services": {"type": "keyword"}, "merged_into": {"type": "keyword"},
    # 工廠版：這件牽涉哪些 lot（異常的 keys.lot；前後站併件時「同一批 lot」優先，investigator/alerts.merge 的 links）
    "lots": {"type": "keyword"},
    # 根因分析重跑：沒有 trace 的件（開件太快、trace 還沒合成完）每隔一陣子重跑，這是跑了幾次
    "rca_attempts": {"type": "integer"},
    # 同源（不合併）：這件卡在哪些外部依賴；跟它卡同一個依賴的其他開著的件
    "dependencies": {"type": "keyword"}, "related_alerts": {"type": "keyword"}, "shared_dependency": {"type": "keyword"},
    # Grafana 的告警只能帶 labels（terms 分組的值），所以標題、摘要、報告連結都存成 keyword
    "title": {"type": "keyword", "ignore_above": 1024}, "summary": {"type": "keyword", "ignore_above": 4096},
    "report_url": {"type": "keyword"}, "report": {"type": "text", "index": False}, "report_at": {"type": "date"},
    # 標題一行＋報告全文：Grafana 表格的 title 欄用它（值對應只顯示第一行），點那格的眼睛（Inspect value）就是全文
    "report_titled": {"type": "text", "index": False},
    "feedback_url": {"type": "keyword"},
    # 結構化的根因分析結果（相似事故檢索、LLM 用；不索引）、相似事故、LLM 摘要
    "findings": {"type": "object", "enabled": False}, "similar": {"type": "object", "enabled": False},
    "llm_summary": {"type": "text", "index": False}, "llm_provider": {"type": "keyword"},
    # case 只做分類（investigator/similar.classify）：像哪一類、信心；回饋表單預帶、Grafana 可依它分組
    "suggested_category": {"type": "keyword"}, "category_confidence": {"type": "double"},
    # 嚴重度怎麼來的（investigator/severity.py）：訊號廣度、持續、換版上限；deploy_at 是這個服務最近一次換版
    "severity_why": {"type": "text", "index": False}, "deploy_at": {"type": "date"},
    # detector detect 每輪寫的 detect-status（detector/cmd_baseline.py）：這輪沒有 baseline 的序列數；Grafana 的 aiops-baseline-missing 查它
    "missing_baseline": {"type": "integer"},
}}}
# 有寫進 _source、但沒進 mapping 的欄位（dynamic false：存得下、查不到）：api POST /api/rca 標的，下一輪 alert 看過就拿掉
UNMAPPED_FIELDS = ("rca_requested", "rca_requested_at")

AlertStatus = Literal["open", "resolved"]
_LIST_FIELDS = ("anomaly_ids", "series", "routes", "lots", "upstream_services", "similar", "dependencies", "related_alerts", "shared_dependency")


class Alert(Doc):
    """aiops-alerts 的一件。欄位的意思見 docs/aiops-data-reference.md「aiops-alerts」。
    誰寫哪些：alerts.merge 開件、併件（alert_id、category、service、status、時間、anomaly_ids、series、routes、lots、upstream_services、severity）；
    cmd_alert 補歸屬（dept、group）、嚴重度（severity、severity_why、deploy_at）、根因分析（report*、findings、similar、suggested_category、
    category_confidence、llm_*、rca_attempts、report_at）、同源（dependencies、related_alerts、shared_dependency）、標題與摘要；api 標 rca_requested。"""
    timestamp: datetime | None = Field(default=None, alias="@timestamp")
    alert_id: str
    category: str
    service: str
    status: AlertStatus
    started_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    severity: str = "warning"
    dept: str | None = None
    group: str | None = None
    anomaly_ids: list[str] = []
    series: list[str] = []
    routes: list[str] = []
    lots: list[str] = []
    upstream_services: list[str] = []
    merged_into: str | None = None
    title: str | None = None
    summary: str | None = None
    report: str | None = None
    report_titled: str | None = None
    report_url: str | None = None
    feedback_url: str | None = None
    report_at: datetime | None = None
    rca_attempts: int | None = None
    rca_requested: bool | None = None
    rca_requested_at: datetime | None = None
    findings: dict | None = None
    similar: list[dict] = []
    suggested_category: str | None = None
    category_confidence: float | None = None
    llm_summary: str | None = None
    llm_provider: str | None = None
    severity_why: str | None = None
    deploy_at: datetime | None = None
    dependencies: list[str] = []
    related_alerts: list[str] = []
    shared_dependency: list[str] = []
    anomaly_count: int | None = None
    missing_baseline: int | None = None          # 只有 detect-status 那筆有；列在這裡是為了跟 mapping 對得上
    # 這一輪算的，不寫 ES（exclude）
    anoms: list[dict] = Field(default_factory=list, exclude=True)      # 這一輪併進來的異常（含 _id）
    new: bool = Field(default=False, exclude=True)                      # 這一輪新開的
    upgraded: bool = Field(default=False, exclude=True)                 # 這一輪嚴重度升級了
    rerun: bool = Field(default=False, exclude=True)                    # 這一輪重跑了根因分析
    report_text: str | None = Field(default=None, exclude=True)         # 這一輪的報告全文（寫 ES 前加回饋連結成 report）
    llm: dict | None = Field(default=None, exclude=True)                # 這一輪的 LLM 摘要（summarize 的回傳）

    MAX_IDS: ClassVar[int] = 500          # 一件最多記幾筆異常 id（很長的事故不要讓文件無限長）

    @field_validator(*_LIST_FIELDS, mode="before")
    @classmethod
    def _lists(cls, v):
        return [] if v is None else list(v)

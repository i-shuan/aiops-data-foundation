"""⑥⑧ aiops-alerts 的契約：index 名、mapping、@timestamp 往前寫的規則（stamp_of）。
誰寫：investigator/cmd_alert（一件一筆、心跳）、detector/cmd_baseline（detect-status）；誰讀：api/（表單、清單）、feedback/（量化）、Grafana。
寫的人和讀的人在不同階段，所以放 core/。Alert 模型見同一份 PDCA 的 D。"""
from __future__ import annotations

from datetime import datetime, timedelta

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

"""所有 aiops-* 索引的名字與 mapping，一份定義在這裡（docs/pdca/meta/2026-11-layering.md）。

Store 的三種實作（memory、file、es）索引名與文件格式都一樣，同一批結果搬到 ES 不用轉。
detector／investigator 各模組直接 `from sources.schema import ANOMALY_INDEX` 等。
"""
from __future__ import annotations

from datetime import datetime, timedelta

BASELINE_INDEX = "aiops-baseline"
ANOMALY_INDEX = "aiops-anomalies"
TUNING_INDEX = "aiops-tuning"
INCIDENT_INDEX = "aiops-incidents"
FEEDBACK_INDEX = "aiops-feedback"
ALERT_INDEX = "aiops-alerts"
CHRONIC_INDEX = "aiops-chronic"
SEGMENT_INDEX = "aiops-trace-segments"

# ── baseline、anomalies ──
# baseline 讀回來的欄位。method 存成 baseline_method：序列的 keys 也可能有 method（HTTP method），會撞名
BASELINE_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution", "baseline_method", "baseline_source", "trained_at")
_RENAMED = {"baseline_method": "method"}
_GROUP_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution")

_KEYS_MAPPING = {"type": "flattened"}
BASELINE_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "series": {"type": "keyword"}, "keys": _KEYS_MAPPING, "dow": {"type": "byte"}, "hour": {"type": "byte"},
    "level": {"type": "keyword"}, "median": {"type": "double"}, "mad": {"type": "double"},
    "p05": {"type": "double"}, "p95": {"type": "double"}, "n": {"type": "integer"},
    "trained_at": {"type": "date"}, "history_from": {"type": "date"}, "history_to": {"type": "date"},
    "resolution": {"type": "keyword"}, "method": {"type": "keyword"}, "baseline_source": {"type": "keyword"},
    "events": {"type": "double"},
}}}
ANOMALY_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "series": {"type": "keyword"}, "keys": _KEYS_MAPPING,
    "value": {"type": "double"}, "median": {"type": "double"}, "expected_low": {"type": "double"},
    "expected_high": {"type": "double"}, "z": {"type": "double"}, "level": {"type": "keyword"},
    "direction": {"type": "keyword"}, "detected_at": {"type": "date"},
    "resolution": {"type": "keyword"}, "method": {"type": "keyword"}, "baseline_source": {"type": "keyword"},
    "confidence": {"type": "keyword"}, "p": {"type": "double"},
    "status": {"type": "keyword"}, "streak": {"type": "integer"},
    "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "alert_id": {"type": "keyword"},   # 併進哪一件（investigator alert 寫回；儀表板依它撈這件的異常）
    # 哪一版 engine 產生的（core/version.py；owner.write_anomalies 補）。放在這裡而不是 RULE_FIELDS：index 是 dynamic false，
    # 但 ensure_index 對既有 index 也會 PUT _mapping 把這份 properties 補上，detect、rules、peer、quality、trace 每輪都呼叫，不用另外補
    "engine_version": {"type": "keyword"},
}}}
# 規則、peer、quality、trace 寫異常時多補的欄位（PUT _mapping）
RULE_FIELDS = {"severity": {"type": "keyword"}, "message": {"type": "text"}, "rule": {"type": "keyword"}}
# ingest（ML／FDC 判好的異常）再多補的
INGEST_FIELDS = {**RULE_FIELDS, "source": {"type": "keyword"}, "source_id": {"type": "keyword"}, "status": {"type": "keyword"}}

# ── tuning ──
TUNING_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "kind": {"type": "keyword"}, "series": {"type": "keyword"}, "match": {"type": "flattened"},
    "resolution": {"type": "keyword"}, "z": {"type": "double"}, "min_abs": {"type": "double"},
    "confirm_points": {"type": "integer"}, "instant_z": {"type": "double"},
    "start": {"type": "date"}, "end": {"type": "date"}, "reset_at": {"type": "date"}, "warmup": {"type": "keyword"},
    "apply_to": {"type": "keyword"},
    "schedule": {"type": "flattened"}, "effect": {"type": "flattened"},
    "status": {"type": "keyword"}, "source": {"type": "keyword"}, "expires_at": {"type": "date"},
    "reason": {"type": "text"}, "by": {"type": "keyword"}, "at": {"type": "date"},
    "approved_by": {"type": "keyword"}, "approved_at": {"type": "date"},
    "backtest": {"type": "object", "enabled": False},     # backtest 指令寫的結果（docs/pdca/phase5-learning-loop/2026-10-loop-update.md）
    "rule_draft": {"type": "object", "enabled": False}}}}

# ── feedback、incidents ──
FEEDBACK_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "anomaly_id": {"type": "keyword"}, "anomaly_at": {"type": "date"},
    "series": {"type": "keyword"}, "keys": {"type": "flattened"}, "verdict": {"type": "keyword"},
    "incident_id": {"type": "keyword"}, "correct_root_cause": {"type": "text"}, "note": {"type": "text"},
    "by": {"type": "keyword"}, "alert_id": {"type": "keyword"}, "alert_started_at": {"type": "date"},
    "fp_reason": {"type": "keyword"},
    # 自由文字＋LLM 抽出來的四欄（docs/pdca/phase5-learning-loop/2026-10-feedback-nl-extract.md）；extract.ok false 就只有原文
    "feedback_text": {"type": "text"},
    "extract": {"properties": {"ok": {"type": "boolean"}, "provider": {"type": "keyword"}, "reason": {"type": "text"},
                               "feedback_type": {"type": "keyword"}, "identified_cause": {"type": "keyword"},
                               "target_service": {"type": "keyword"}, "suggested_action": {"type": "keyword"}}},
    "tuning_id": {"type": "keyword"},      # 抽取結果寫進 aiops-tuning 的那一筆（proposed）
    # 採納率的兩題：可能根因／建議的排查動作有沒有參考價值（yes、no、not_seen）
    "rca_cause_useful": {"type": "keyword"}, "rca_action_useful": {"type": "keyword"}}}}
# status：open／resolved（人補登的）、auto（事件整合自動開的，還沒人看）、confirmed（老師傅標真的）、corrected（真的但根因改過）
# 相似事故檢索只用 confirmed、corrected（investigator/similar/similar.py）。根因分類表在 plugin（core/domain.categories）
INCIDENT_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "title": {"type": "text"}, "start": {"type": "date"}, "end": {"type": "date"},
    "services": {"type": "keyword"}, "severity": {"type": "keyword"}, "status": {"type": "keyword"},
    "root_cause": {"type": "text"}, "action": {"type": "text"}, "anomaly_ids": {"type": "keyword"},
    "detected_at": {"type": "date"}, "resolved_at": {"type": "date"}, "by": {"type": "keyword"},
    # 老師傅的經驗（docs/pdca/phase5-learning-loop/2026-10-expert-feedback.md）
    "alert_id": {"type": "keyword"}, "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "root_cause_category": {"type": "keyword"}, "how_detected": {"type": "text"}, "checked": {"type": "keyword"},
    "improve": {"type": "text"}, "confirmed_at": {"type": "date"}, "confirmed_by": {"type": "keyword"},
    # 歷史事故單匯入（investigator/similar/importer.py）：source import；note 是匯入檔的備註
    "source": {"type": "keyword"}, "note": {"type": "text"},
    # 指紋：相似事故檢索用（investigator/similar/similar.py）
    "fingerprint": {"properties": {"services": {"type": "keyword"}, "routes": {"type": "keyword"}, "series": {"type": "keyword"},
                                   "slow_segments": {"type": "keyword"}, "error_origins": {"type": "keyword"},
                                   "patterns": {"type": "text"}}}}}}

# ── alerts ──
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
    # 工廠版：這件牽涉哪些 lot（異常的 keys.lot；前後站併件時「同一批 lot」優先，investigator/alerts/merge.merge 的 links）
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
    # 嚴重度怎麼來的（investigator/alerts/severity.py）：訊號廣度、持續、換版上限；deploy_at 是這個服務最近一次換版
    "severity_why": {"type": "text", "index": False}, "deploy_at": {"type": "date"},
    # detector detect 每輪寫的 detect-status（detector/baseline/detect.py）：這輪沒有 baseline 的序列數；Grafana 的 aiops-baseline-missing 查它
    "missing_baseline": {"type": "integer"},
}}}

# ── chronic、trace-segments ──
CHRONIC_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "week": {"type": "keyword"}, "source": {"type": "keyword"}, "rank": {"type": "integer"},
    "keys": {"type": "flattened"}, "days": {"type": "integer"}, "total": {"type": "long"}, "per_day": {"type": "double"},
    "first_day": {"type": "date"}, "last_day": {"type": "date"}, "owner": {"type": "keyword"}, "message": {"type": "text"}}}}
SEGMENT_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "kind": {"type": "keyword"},
    "entry": {"properties": {"service": {"type": "keyword"}, "name": {"type": "keyword"}}},
    "segment": {"type": "keyword"}, "requests": {"type": "integer"},
    "calls": {"type": "double"}, "time_ms": {"type": "double"}, "error_rate": {"type": "double"},
    "swallowed_rate": {"type": "double"}, "exception_type": {"type": "keyword"},
    "caller": {"type": "keyword"}, "callee": {"type": "keyword"}, "skew_ms": {"type": "double"},
}}}

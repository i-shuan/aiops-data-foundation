"""⑨ 回饋（aiops-feedback）的索引與 mapping。一筆回饋對一筆異常（或一件告警、或一次漏報）；計算在 service/feedback/suggest.py。"""
from __future__ import annotations

FEEDBACK_INDEX = "aiops-feedback"
VERDICTS = ("true_positive", "false_positive", "wrong_root_cause", "missed")
RCA_USEFUL = ("yes", "no", "not_seen")   # 採納率的兩題（rca_cause_useful、rca_action_useful）
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
    "rca_cause_useful": {"type": "keyword"}, "rca_action_useful": {"type": "keyword"},
}}}

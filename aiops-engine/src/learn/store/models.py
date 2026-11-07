"""⑨ 學習迴圈的資料物件與 mapping：

- aiops-tuning：Adjustment（一筆調整／建議；寫進去之前驗一次）。誰寫：learn/（tune、adjust、feedback suggest／--text、incident --exclude-training）、detect 的 level_shift 提的 reset_point；誰讀：detect/series_ops/manual_adjust.py（train、detect、rules 套用）。寫的人和讀的人在不同階段，所以放這裡。
- aiops-feedback：一筆回饋對一筆異常（或一件告警、或一次漏報）；計算在 learn/param_suggester.py。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict

from common.infra.es import Doc

TUNING_INDEX = "aiops-tuning"

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


Kind = Literal["override", "exclude_window", "reset_point", "recurring_override", "rule_suggestion"]


class Adjustment(Doc):
    """aiops-tuning 的一筆。五種 kind 各用一部分欄位（adjust.problem 判哪種缺什麼）；寫的人在三個階段（tune／adjust／shift 提的、
    feedback 提的、incident 提的），讀的人是 detector/adjust.py（套用）、feedback/（回測、核准）。
    這裡的用途是**寫進去之前驗**：kind、status 只能是這幾個值、有型別的欄位型別要對；不認得的鍵先放行（extra allow）——
    五種建議帶的附屬欄位（missing、incident_ids、feedback_text…）還沒全部盤完，盤完再收成 forbid（docs/pdca/meta/2026-11-clean-code.md Act）。
    讀的那一端（adjust.load → list[dict]）這一輪沒改：套用是 pandas 向量運算，物件沒有好處。
    跟 core/config_models.Adjustment（yaml 那邊）欄位名一樣、型別不同：yaml 可以寫相對時間（When、Dur），ES 存的是算好的 ISO 字串。"""
    model_config = ConfigDict(extra="allow", populate_by_name=True, validate_assignment=True)

    kind: Kind = "override"
    status: Literal["active", "proposed"] = "active"
    series: str | None = None
    match: dict[str, Any] | None = None
    source: str | None = None
    reason: str | None = None
    by: str | None = None
    at: str | None = None
    expires_at: str | None = None
    # override
    resolution: str | None = None
    z: float | None = None
    min_abs: float | None = None
    min_count: int | None = None
    confirm_points: int | None = None
    instant_z: float | None = None
    # exclude_window
    start: str | None = None
    end: str | None = None
    apply_to: list[str] | None = None
    # reset_point
    reset_at: str | None = None
    warmup: str | None = None
    # recurring_override
    schedule: dict[str, Any] | None = None
    effect: dict[str, Any] | None = None
    # rule_suggestion
    service: str | None = None
    root_cause_category: str | None = None
    rule_draft: dict[str, Any] | None = None
    # 核准、回測寫回的
    approved_by: str | None = None
    approved_at: str | None = None
    backtest: dict[str, Any] | None = None


def validated(doc: dict) -> dict:
    """寫 aiops-tuning 前過一次 Adjustment：kind／status 給錯、型別不對在這裡就丟 ValidationError，不會寫進去變成 adjust.problem 才發現的壞資料。"""
    return Adjustment.model_validate(doc).to_doc()


# ── aiops-feedback ──
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

"""④⑨ aiops-tuning 的契約：index 名與 mapping（文件模型見 docs/pdca/meta/2026-11-clean-code.md D）。
誰寫：feedback/（tune、adjust、feedback suggest／--text、incident --exclude-training、shift 提的 reset_point）；誰讀：detector/adjust.py（train、detect、rules 套用）。
寫的人和讀的人在不同階段，所以放 core/。"""
from __future__ import annotations

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

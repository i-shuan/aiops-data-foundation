"""aiops-tuning：使用者調整（tune）、排除時段／重置點／週期放寬（adjust）、回饋提的建議都寫這裡；train、detect 讀它。"""
from __future__ import annotations

import pandas as pd

from . import adjust as A


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

def load_adjustments(es, cfg: dict) -> list[dict]:
    """全部調整（四種 kind）：設定檔（overrides、adjustments_file）＋ aiops-tuning（tune、adjust 指令、之後的 learning loop）。"""
    return A.load(es, TUNING_INDEX, cfg)

def load_overrides(es, cfg: dict, now=None) -> list[dict]:
    """粒度、z、min_abs、confirm_points、instant_z 的調整（kind: override；舊資料沒有 kind 也是），生效中的，後面蓋前面。"""
    return A.active(load_adjustments(es, cfg), now if now is not None else pd.Timestamp.now(tz="UTC"), kinds=("override",))

def data_adjustments(es, cfg: dict, now) -> list[dict]:
    """排除時段、重置點、週期放寬，生效中的。"""
    return A.active(load_adjustments(es, cfg), now, kinds=("exclude_window", "reset_point", "recurring_override"))


def propose_from_text(es, fields: dict, alert: dict, text: str, by: str | None, now, current_z: float = 5.0) -> str | None:
    """回饋文字抽出來的四欄對得到調整種類就寫一筆 aiops-tuning（feedback.suggest_from_text；proposed、source feedback、reason 帶原文），
    回傳 id；suggested_action 是 none 回 None。同一個人對同一件、同一種 kind 再送會覆蓋，不重複。不自動生效（adjust approve 才會）。"""
    from core.es import doc_id
    from . import feedback as F
    sug = F.suggest_from_text(fields, alert, text, current_z)
    if not sug:
        return None
    sug = {k: v for k, v in sug.items() if v not in (None, "", [])}
    sug.update({"at": now.isoformat(), "by": by or "feedback"})
    _id = doc_id("feedback-text", alert.get("alert_id"), by or "", sug["kind"])
    es.ensure_index(TUNING_INDEX, TUNING_MAPPING)
    es.req("PUT", f"/{TUNING_INDEX}/_doc/{_id}?refresh=true", sug)
    return _id

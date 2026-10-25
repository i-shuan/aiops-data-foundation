"""aiops-tuning：使用者調整（tune）、排除時段／重置點／週期放寬（adjust）、回饋提的建議都寫這裡；train、detect 讀它。"""
from __future__ import annotations

import pandas as pd

from . import adjust as A


TUNING_INDEX = "aiops-tuning"

TUNING_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "kind": {"type": "keyword"}, "series": {"type": "keyword"}, "match": {"type": "flattened"},
    "resolution": {"type": "keyword"}, "z": {"type": "double"}, "min_abs": {"type": "double"},
    "start": {"type": "date"}, "end": {"type": "date"}, "reset_at": {"type": "date"}, "warmup": {"type": "keyword"},
    "apply_to": {"type": "keyword"},
    "schedule": {"type": "flattened"}, "effect": {"type": "flattened"},
    "status": {"type": "keyword"}, "source": {"type": "keyword"}, "expires_at": {"type": "date"},
    "reason": {"type": "text"}, "by": {"type": "keyword"}, "at": {"type": "date"}}}}

def load_adjustments(es, cfg: dict) -> list[dict]:
    """全部調整（四種 kind）：設定檔（overrides、adjustments_file）＋ aiops-tuning（tune、adjust 指令、之後的 learning loop）。"""
    return A.load(es, TUNING_INDEX, cfg)

def load_overrides(es, cfg: dict) -> list[dict]:
    """粒度、z、min_abs 的調整（kind: override；舊資料沒有 kind 也是），生效中的，後面蓋前面。"""
    return A.active(load_adjustments(es, cfg), pd.Timestamp.now(tz="UTC"), kinds=("override",))

def data_adjustments(es, cfg: dict, now) -> list[dict]:
    """排除時段、重置點、週期放寬，生效中的。"""
    return A.active(load_adjustments(es, cfg), now, kinds=("exclude_window", "reset_point", "recurring_override"))

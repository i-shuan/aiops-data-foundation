"""慢性病清單（aiops-chronic）：每週一份，天天都有的告警、log（service/detect/chronic.py）。"""
from __future__ import annotations

from core.repository._base import Repository

CHRONIC_INDEX = "aiops-chronic"
CHRONIC_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "week": {"type": "keyword"}, "source": {"type": "keyword"}, "rank": {"type": "integer"},
    "keys": {"type": "flattened"}, "days": {"type": "integer"}, "total": {"type": "long"}, "per_day": {"type": "double"},
    "first_day": {"type": "date"}, "last_day": {"type": "date"}, "owner": {"type": "keyword"}, "message": {"type": "text"}}}}


class ChronicRepository(Repository):
    INDEX, MAPPING = CHRONIC_INDEX, CHRONIC_MAPPING

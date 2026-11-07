"""llm：LLM 摘要（只加註、每句附依據；provider 由 LLM_PROVIDER 決定）。
演算法在 incident/alert_service.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 llm_summary。"""
from __future__ import annotations

from incident.alert_service import llm_summary as run

__all__ = ["run"]

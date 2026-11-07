"""from_text：回饋的一句話：LLM 抽成調整（送出回饋時）。
演算法在 learn/feedback_service.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 propose_from_text。"""
from __future__ import annotations

from learn.feedback_service import propose_from_text as run

__all__ = ["run"]

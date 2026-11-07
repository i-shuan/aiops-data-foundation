"""precision：整條序列的準確率：太低放寬、太高收緊。
演算法在 learn/feedback_service.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 suggest_precision。"""
from __future__ import annotations

from learn.feedback_service import suggest_precision as run

__all__ = ["run"]

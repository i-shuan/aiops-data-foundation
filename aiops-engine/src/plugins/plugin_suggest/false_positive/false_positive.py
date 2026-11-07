"""false_positive：誤報：放寬門檻、時段放寬、至少差多少才報。
演算法在 learn/feedback_service.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 suggest_false_positives。"""
from __future__ import annotations

from learn.feedback_service import suggest_false_positives as run

__all__ = ["run"]

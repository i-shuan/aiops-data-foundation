"""rule_upgrade：同根因確認 N 次：升級成靜態臨界值規則。
演算法在 learn/feedback_service.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 suggest_rules。"""
from __future__ import annotations

from learn.feedback_service import suggest_rules as run

__all__ = ["run"]

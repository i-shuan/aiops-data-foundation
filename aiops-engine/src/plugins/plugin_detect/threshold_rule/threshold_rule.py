"""threshold_rule：靜態臨界值規則：一定不能超過的線。
演算法在 detect/strategy/threshold_rule.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 cmd_rules。"""
from __future__ import annotations

from detect.strategy.threshold_rule import cmd_rules as run

__all__ = ["run"]

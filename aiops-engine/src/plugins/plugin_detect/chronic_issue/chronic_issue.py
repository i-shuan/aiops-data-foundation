"""chronic_issue：慢性病清單：天天噴、沒人修的（每週）。
演算法在 detect/strategy/chronic_issue.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 cmd_chronic。"""
from __future__ import annotations

from detect.strategy.chronic_issue import cmd_chronic as run

__all__ = ["run"]

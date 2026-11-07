"""peer_compare：Pod 同儕橫向比對：同服務的 Pod 只有一隻不對勁。
演算法在 detect/strategy/peer_compare.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 cmd_peer。"""
from __future__ import annotations

from detect.strategy.peer_compare import cmd_peer as run

__all__ = ["run"]

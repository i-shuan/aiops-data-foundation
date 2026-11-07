"""same_entity：同一個對象（服務）、時間接著來的合成一件；跨服務、前後站照情境的 chain。
演算法在 incident/alert_grouper.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 merge。"""
from __future__ import annotations

from incident.alert_grouper import merge as run

__all__ = ["run"]

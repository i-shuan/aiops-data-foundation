"""fields：欄位比對：同一段變慢、同一個錯誤源頭、同服務…（看得出為什麼像）。
演算法在 llm/similar.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 field_candidates。"""
from __future__ import annotations

from llm.similar import field_candidates as run

__all__ = ["run"]

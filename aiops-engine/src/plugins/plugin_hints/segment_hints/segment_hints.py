"""segment_hints：線索：這次已經看到變化、過去也出事過的段，件數多的排前面。
演算法在 llm/similar.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 hint_lines。"""
from __future__ import annotations

from llm.similar import hint_lines as run

__all__ = ["run"]

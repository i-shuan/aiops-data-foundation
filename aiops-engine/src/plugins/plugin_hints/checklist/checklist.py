"""checklist：先查什麼：同一類事故老師傅勾過的排前面，不夠用情境的 checklists 補。
演算法在 llm/similar.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 checklist_lines。"""
from __future__ import annotations

from llm.similar import checklist_lines as run

__all__ = ["run"]

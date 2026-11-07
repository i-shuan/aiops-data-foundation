"""vector：向量輔助：說法不同但意思一樣（要設 EMBEDDING_URL、EMBEDDING_MODEL）。
演算法在 llm/similar.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 vector_candidates。"""
from __future__ import annotations

from llm.similar import vector_candidates as run

__all__ = ["run"]

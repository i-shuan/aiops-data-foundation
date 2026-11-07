"""level_shift：一階跳變：停在新水準（含只判往上的往下掉）→ 提重新學。
演算法在 detect/series_ops/level_shift.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 cmd_shift。"""
from __future__ import annotations

from detect.series_ops.level_shift import cmd_shift as run

__all__ = ["run"]

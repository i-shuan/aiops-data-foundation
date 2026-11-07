"""quality_check：資料品質檢查：路由沒參數化、欄位缺、資料停了。
演算法在 detect/series_ops/quality_check.py（主程式其他地方也在用，留在原層）；這個外掛的 run 就是 cmd_quality。"""
from __future__ import annotations

from detect.series_ops.quality_check import cmd_quality as run

__all__ = ["run"]

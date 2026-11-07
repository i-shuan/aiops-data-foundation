"""dynamic_baseline：動態基準線學習：每條序列 × 星期幾 × 小時的平常值，偏太多就報。
演算法在 detect/detect_service.py（回測、level_shift、trace 也在用，留在原層）。run＝detect；train、tune 是同一個外掛的另外兩個指令。"""
from __future__ import annotations

from detect.detect_service import cmd_detect as run
from detect.detect_service import cmd_train as train
from detect.detect_service import cmd_tune as tune

__all__ = ["run", "train", "tune"]

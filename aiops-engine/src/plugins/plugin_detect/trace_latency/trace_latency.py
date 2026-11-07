"""trace_latency：trace 每一段的統計與偵測。
演算法在 detect/trace/latency_stats.py。run＝trace-detect；trace-collect、trace-train 是同一個外掛的另外兩個指令。"""
from __future__ import annotations

from detect.trace.latency_stats import cmd_trace_collect as collect
from detect.trace.latency_stats import cmd_trace_detect as run
from detect.trace.latency_stats import cmd_trace_train as train

__all__ = ["collect", "run", "train"]

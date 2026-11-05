"""detector 的 trace-collect／trace-train／trace-detect／trace-merge（IT 專用，由 observability plugin 的 commands() 掛上）。
引擎在 detector/trace.py、trace_merge.py、cmd_trace.py（階段 C 搬進來）；設定模型 config.py。"""
from __future__ import annotations

import typer

from detector.cli import Opt, run
from detector.cmd_trace import cmd_trace_collect, cmd_trace_detect, cmd_trace_merge, cmd_trace_train

from .config import TraceConfig


def trace_collect(config: str = Opt(...), start: str | None = Opt(None), end: str | None = Opt(None)) -> int:
    return run(cmd_trace_collect, TraceConfig, locals())


def trace_train(config: str = Opt(...), history: str | None = Opt(None, help="學多久的資料，例如 4w、2h（預設 history_weeks）"),
                end: str | None = Opt(None)) -> int:
    return run(cmd_trace_train, TraceConfig, locals())


def trace_detect(config: str = Opt(...), window: str = Opt("30min"), at: str | None = Opt(None)) -> int:
    return run(cmd_trace_detect, TraceConfig, locals())


def trace_merge(config: str = Opt(...), window: str = Opt("3min", help="第一次（沒有上次合到哪的紀錄）往前合多久"),
                at: str | None = Opt(None)) -> int:
    return run(cmd_trace_merge, TraceConfig, locals())


def commands() -> dict:
    """{子命令名: register(app)}。"""
    return {
        "trace-collect": lambda app: app.command("trace-collect")(trace_collect),
        "trace-train": lambda app: app.command("trace-train")(trace_train),
        "trace-detect": lambda app: app.command("trace-detect")(trace_detect),
        "trace-merge": lambda app: app.command("trace-merge")(trace_merge),
    }

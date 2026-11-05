"""detector chronic（慢性病清單；IT 專用，由 observability plugin 的 commands() 掛上）。
引擎在 detector/chronic.py、cmd_chronic.py（階段 C 搬進來）；設定模型 config.py。"""
from __future__ import annotations

from detector.cli import Opt, run
from detector.cmd_chronic import cmd_chronic

from .config import ChronicConfig


def chronic(config: str = Opt(...), at: str | None = Opt(None, help="回測：拿過去某個時間點當成現在")) -> int:
    return run(cmd_chronic, ChronicConfig, locals())


def register(app) -> None:
    app.command("chronic", help="慢性病清單：天天噴錯、沒人修的（每週）")(chronic)

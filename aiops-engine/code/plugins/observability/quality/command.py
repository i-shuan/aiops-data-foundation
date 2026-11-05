"""detector quality（資料品質檢查；IT 專用，由 observability plugin 的 commands() 掛上）。
引擎在 detector/quality.py、cmd_quality.py（階段 C 搬進來）；設定模型 config.py。"""
from __future__ import annotations

from detector.cli import Opt, run
from detector.cmd_quality import cmd_quality

from .config import QualityConfig


def quality(config: str = Opt(...), at: str | None = Opt(None, help="回測：拿過去某個時間點當成現在"),
            only: str | None = Opt(None, help="只跑這幾個（逗號分開）")) -> int:
    return run(cmd_quality, QualityConfig, locals())


def register(app) -> None:
    app.command("quality")(quality)

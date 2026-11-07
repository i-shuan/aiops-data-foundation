"""命令列共用的三樣：登記子命令的裝飾器（registry）、typer 參數 → Namespace → cmd_*(config, args) 的轉接（run）、typer.Option 的短名（Opt）。
interface/cli/detect.py、feedback.py、情境加的子命令都用這一套，三邊登記出來的形狀一樣：{子命令名: register(app)}。"""
from __future__ import annotations

from argparse import Namespace
from typing import Callable

import pandas as pd
import typer

from common.clock import parse_duration, parse_instant
from common.config import load_config

SERIES = "configs/baseline/series.yaml"
Opt = typer.Option


def _bad(parse):
    def parser(text: str):
        try:
            return parse(text)
        except ValueError as e:
            raise typer.BadParameter(str(e)) from None
    return parser


_instant = _bad(parse_instant)
_duration = _bad(lambda s: pd.Timedelta(parse_duration(s)))


def at_opt(help: str | None = None):
    """時間點的選項（--at、--end…）：在這裡把字串解析成帶時區的 pandas.Timestamp，往下傳的不是字串。不給＝None（用現在）。"""
    return Opt(None, parser=_instant, metavar="TIME", help=help)


def window_opt(default: str, help: str | None = None):
    """一段時間長度的選項（--window）：解析成 pandas.Timedelta。"""
    return Opt(default, parser=_duration, metavar="DURATION", help=help)


def registry(commands: dict[str, Callable[[typer.Typer], None]]):
    """回一個裝飾器：把被裝飾的函式登記成子命令（參數就是命令列參數）。"""
    def command(name: str, help: str | None = None):
        def deco(fn):
            commands[name] = lambda app: app.command(name, help=help)(fn)
            return fn
        return deco
    return command


def run(handler, kind: str, /, **params) -> int:
    """typer 的參數（明確列出、不用 locals()）→ argparse.Namespace（屬性名跟以前一樣；id_、list_ 去掉底線），
    讀設定（core.config，kind 挑模型），交給 handler(config, args)。前兩個是 positional-only：命令自己的參數也可以叫 kind。"""
    args = Namespace(**{k.rstrip("_"): v for k, v in params.items()})
    return handler(load_config(args.config, kind), args)


def group(name: str, help: str | None = None) -> typer.Typer:
    """有子命令的命令（feedback add／list…）：一個子 app，掛上去時用 app.add_typer。沒給子命令就印用法、回 2。"""
    return typer.Typer(name=name, help=help, add_completion=False, rich_markup_mode=None, pretty_exceptions_enable=False, no_args_is_help=True)

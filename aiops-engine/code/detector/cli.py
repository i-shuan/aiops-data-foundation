"""detector 子命令共用的膠水：typer 參數 → argparse.Namespace → 讀設定 → 交給 cmd_*(cfg, args)。
內建子命令（detector/__main__.py）與 plugin 加的子命令（plugins/*/…/command.py）都用這裡的 run，兩邊同一種形狀。"""
from __future__ import annotations

from argparse import Namespace

import typer

from core.config import load

Opt = typer.Option


def run(handler, model, params: dict) -> int:
    """typer 的參數 → argparse.Namespace（屬性名跟以前一樣；id_、list_ 去掉底線），讀設定（core.config.load，model 是這種設定檔的模型），
    交給 cmd_*(cfg, args)。"""
    args = Namespace(**{k.rstrip("_"): v for k, v in params.items()})
    return handler(load(args.config, model), args)

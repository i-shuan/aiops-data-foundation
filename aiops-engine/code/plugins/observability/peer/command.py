"""detector peer（Pod 同儕橫向比對；IT 專用，由 observability plugin 的 commands() 掛上）。
引擎在 detector/peer.py、cmd_peer.py（階段 C 搬進來）；設定模型 config.py。"""
from __future__ import annotations

from detector.cli import Opt, run
from detector.cmd_peer import cmd_peer

from .config import PeerConfig


def peer(config: str = Opt(...), at: str | None = Opt(None, help="回測：拿過去某個時間點當成現在"),
         only: str | None = Opt(None, help="只跑這幾個（逗號分開）")) -> int:
    return run(cmd_peer, PeerConfig, locals())


def register(app) -> None:
    app.command("peer")(peer)

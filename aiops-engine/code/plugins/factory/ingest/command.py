"""detector ingest（別人判好的異常接進 ⑤；工廠、只有指標兩個 plugin 的 commands() 掛上）。
引擎在 detector/ingest.py、cmd_ingest.py（階段 C 搬進來）；設定模型 config.py。"""
from __future__ import annotations

from detector.cli import Opt, run
from detector.cmd_ingest import cmd_ingest

from .config import IngestConfig


def ingest(config: str = Opt(..., help="configs/factory/ingest.yaml"),
           window: str = Opt("10m", help="看最近多久的來源列（預設 10m；_id 依來源識別碼，重讀不重複）"),
           at: str | None = Opt(None, help="拿某個時間點當成現在")) -> int:
    return run(cmd_ingest, IngestConfig, locals())


def register(app) -> None:
    app.command("ingest", help="別人判好的異常（ML 的異常跳點、FDC 鳴叫）→ aiops-anomalies（detector/ingest.py）")(ingest)

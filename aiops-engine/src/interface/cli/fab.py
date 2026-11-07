"""工廠情境的命令列：paths-merge（MES 進出站＋S6F11 → 一個 lot 一筆履歷；計算在 detect/trace/path_service.py）、
ingest（ML／FDC 判好的異常 → aiops-anomalies；計算在 detect/fab_ingest/ingest_service.py）。排程器每 5 分鐘、每分鐘各跑一次。"""
from __future__ import annotations

from argparse import Namespace

import pandas as pd
import typer

import detect.trace.path_service as PS
from detect.fab_ingest import ingest_service as ING
from common.infra.clock import now_ts as _now
from common.infra.es import ES
from common.infra.repos import Repos
from common.scenario import load_scenario
from common.config import load_config
from detect.config.ingest import IngestConfig
from detect.config.paths import PathsConfig


def register(app: typer.Typer) -> None:
    """掛成 detector 的子命令（common/scenario/factory.yaml 的 commands）。"""
    app.command("paths-merge", help="工廠：MES 進出站＋S6F11 機台事件 → 一個 lot 一筆履歷 lot-paths-*（plugins/factory/paths.py）")(paths_merge)


def paths_merge(config: str = typer.Option(..., help="configs/factory/paths.yaml"),
                window: str = typer.Option("1d", help="最近多久有進站／出站的 lot 重合成（預設 1d）"),
                at: str | None = typer.Option(None, help="拿某個時間點當成現在")) -> int:
    return cmd_paths_merge(load_config(config, "paths"), Namespace(config=config, window=window, at=at))


def cmd_paths_merge(paths_config: PathsConfig, args) -> int:
    now = _now(args.at)
    n = PS.run(Repos(ES(), load_scenario()), paths_config, now, pd.Timedelta(args.window))
    tz = paths_config.timezone
    print(f"{now.tz_convert(tz):%m-%d %H:%M}：paths-merge 最近 {args.window} 有動的 lot，合了 {n} 個")
    return 0


def cmd_ingest(ingest_config: IngestConfig, args) -> int:
    now = _now(args.at)
    tz = ingest_config.timezone
    got = ING.run(Repos(ES(), load_scenario()), ingest_config, now, pd.Timedelta(args.window))
    for name, (n, skipped) in got.items():
        print(f"  {name}：{n} 筆異常" + (f"（略過 {skipped} 筆：沒有時間、對象或值）" if skipped else ""))
    print(f"{now.tz_convert(tz):%m-%d %H:%M}：接收 {sum(n for n, _ in got.values())} 筆（最近 {args.window}）")
    return 0

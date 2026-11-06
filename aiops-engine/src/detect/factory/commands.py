"""paths-merge（工廠情境加的 detector 子命令）：MES 進出站＋S6F11 → 一個 lot 一筆（lot-paths-*；plugins/factory/paths.py）。
排程器每 5 分鐘跑：最近 --window（預設 1 天）內有進站或出站的 lot 全部重合成一次、整份覆蓋（_id = lot id），
所以晚到的站、剛出站的時間都會補上，重跑不重複。"""
from __future__ import annotations

from argparse import Namespace

import pandas as pd
import typer

from shared.schema.loader import load_config
from shared.connection import ES, get_field
from shared.repos import Repos
from shared.scenario import load_scenario
from shared.clock import now_ts as _now

import detect.factory.paths as P


def _lot_ids(rows: list[dict], lot_field: str) -> set[str]:
    return {str(v) for r in rows if (v := get_field(r, lot_field)) not in (None, "")}


def _rows_of_lots(repo, inp: dict, lot_field: str, lots: set[str]) -> list[dict]:
    """這些 lot 的全部列（含 window 之前的站：一個 lot 的履歷要完整）。"""
    return repo.series.rows_where(inp, lot_field, sorted(lots)) if lots else []


def run(repo, cfg: dict, now: pd.Timestamp, window: pd.Timedelta) -> int:
    """[now − window, now) 有動的 lot 全部重合成。回傳合了幾個 lot。"""
    read_rows = repo.series.rows
    mes, ev = cfg["mes"], cfg.get("events") or {}
    mf = mes["fields"]
    start = now - window
    # MES 列的時間欄位：進站時間（出站晚到的用 end 欄位也查一次）
    recent = read_rows({**mes, "time_field": mf["start"]}, start, now)
    if mf.get("end"):
        recent += read_rows({**mes, "time_field": mf["end"]}, start, now)
    lots = _lot_ids(recent, mf["lot"])
    if not lots:
        return 0
    steps = _rows_of_lots(repo, {**mes, "time_field": mf["start"]}, mf["lot"], lots)
    events = _rows_of_lots(repo, {**ev, "time_field": ev["fields"]["start"]}, ev["fields"]["lot"], lots) if ev else []
    docs = P.build_docs(steps, events, cfg, now.to_pydatetime())
    try:
        repo.housekeeping.put_index_template(P.PATHS_PREFIX, P.PATHS_TEMPLATE)
    except Exception as e:  # noqa: BLE001  沒有 manage_index_templates 權限：template 要請 ES 管理員放（configs/factory/README.md）
        print(f"  放不了 index template {P.PATHS_PREFIX}（{type(e).__name__}）；若索引還沒建，nested 查詢會失敗")
    return repo.traces.write_merged(docs)


def register(app: typer.Typer) -> None:
    """掛成 detector 的子命令（shared/scenario/data/factory.yaml 的 commands）。"""
    app.command("paths-merge", help="工廠：MES 進出站＋S6F11 機台事件 → 一個 lot 一筆履歷 lot-paths-*（plugins/factory/paths.py）")(paths_merge)


def paths_merge(config: str = typer.Option(..., help="configs/factory/paths.yaml"),
                window: str = typer.Option("1d", help="最近多久有進站／出站的 lot 重合成（預設 1d）"),
                at: str | None = typer.Option(None, help="拿某個時間點當成現在")) -> int:
    return cmd_paths_merge(load_config(config, "paths"), Namespace(config=config, window=window, at=at))


def cmd_paths_merge(cfg: dict, args) -> int:
    now = _now(args.at)
    n = run(Repos(ES(), load_scenario()), cfg, now, pd.Timedelta(args.window))
    tz = cfg.get("timezone", "Asia/Taipei")
    print(f"{now.tz_convert(tz):%m-%d %H:%M}：paths-merge 最近 {args.window} 有動的 lot，合了 {n} 個")
    return 0

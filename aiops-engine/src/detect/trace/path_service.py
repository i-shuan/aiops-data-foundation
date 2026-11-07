"""paths-merge 的計算（工廠情境）：MES 進出站＋S6F11 → 一個 lot 一筆（lot-paths-*；合成在 detect/trace/path_builder.py）。
最近 --window（預設 1 天）內有進站或出站的 lot 全部重合成一次、整份覆蓋（_id = lot id），
所以晚到的站、剛出站的時間都會補上，重跑不重複。命令列在 interface/cli/fab.py，排程器每 5 分鐘跑。"""
from __future__ import annotations

import pandas as pd

import detect.trace.path_builder as P
from detect.config.paths import PathsConfig
from common.infra.es import get_field


def _lot_ids(rows: list[dict], lot_field: str) -> set[str]:
    return {str(v) for r in rows if (v := get_field(r, lot_field)) not in (None, "")}


def _rows_of_lots(repo, inp, time_field: str, lot_field: str, lots: set[str]) -> list[dict]:
    """這些 lot 的全部列（含 window 之前的站：一個 lot 的履歷要完整）。"""
    return repo.series.rows_where(inp, lot_field, sorted(lots), time_field) if lots else []


def run(repo, paths_config: PathsConfig, now: pd.Timestamp, window: pd.Timedelta) -> int:
    """[now − window, now) 有動的 lot 全部重合成。回傳合了幾個 lot。"""
    read_rows = repo.series.rows
    mes, ev = paths_config.mes, paths_config.events
    mf = mes.fields
    start = now - window
    # MES 列的時間欄位：進站時間（出站晚到的用 end 欄位也查一次）
    recent = read_rows(mes, start, now, mf.start)
    if mf.end:
        recent += read_rows(mes, start, now, mf.end)
    lots = _lot_ids(recent, mf.lot)
    if not lots:
        return 0
    steps = _rows_of_lots(repo, mes, mf.start, mf.lot, lots)
    events = _rows_of_lots(repo, ev, ev.fields.start, ev.fields.lot, lots) if ev else []
    docs = P.build_docs(steps, events, paths_config, now.to_pydatetime())
    try:
        repo.housekeeping.put_index_template(P.PATHS_PREFIX, P.PATHS_TEMPLATE)
    except Exception as e:  # noqa: BLE001  沒有 manage_index_templates 權限：template 要請 ES 管理員放（configs/factory/README.md）
        print(f"  放不了 index template {P.PATHS_PREFIX}（{type(e).__name__}）；若索引還沒建，nested 查詢會失敗")
    return repo.traces.write_merged(docs)

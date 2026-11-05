"""paths-merge（工廠 plugin 加的 detector 子命令）：MES 進出站＋S6F11 → 一個 lot 一筆（lot-paths-*；plugins/factory/paths/build.py）。
排程器每 5 分鐘跑：最近 --window（預設 1 天）內有進站或出站的 lot 全部重合成一次、整份覆蓋（_id = lot id），
所以晚到的站、剛出站的時間都會補上，重跑不重複。來源是 ES 時直接查索引；設定檔有 source: file 時走 sources 的 Backend（離線跑假資料），結果寫進 store。"""
from __future__ import annotations

from argparse import Namespace

import pandas as pd
import typer

from core.config import load
from core.es_client import ES, get_field
from core.timeutil import _now
from detector.cmd_ingest import read_rows
from sources import as_backend, open_backend

from . import build as P
from .config import PathsConfig


def _lot_ids(rows: list[dict], lot_field: str) -> set[str]:
    return {str(v) for r in rows if (v := get_field(r, lot_field)) not in (None, "")}


def _rows_of_lots(es, inp: dict, lot_field: str, lots: set[str], start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    """這些 lot 的全部列（含 window 之前的站：一個 lot 的履歷要完整）。ES 分批 terms 查；檔案、記憶體來源沒有 terms 查詢，往前讀 60 天再挑。"""
    if not lots:
        return []
    if es.kind != "es":
        rows = read_rows(es, inp, start - pd.Timedelta(days=60), end)
        return [r for r in rows if str(get_field(r, lot_field) or "") in lots]
    out, ids = [], sorted(lots)
    tf = inp.get("time_field", "@timestamp")
    for i in range(0, len(ids), 1000):
        body = {"size": 10000, "query": {"bool": {"filter": [{"terms": {lot_field: ids[i:i + 1000]}}]}}, "sort": [{tf: "asc"}, {"_doc": "asc"}]}
        out += [h["_source"] for h in es.raw_search(inp["index"], body).get("hits", {}).get("hits", [])]
    return out


def run(es, cfg: dict, now: pd.Timestamp, window: pd.Timedelta) -> int:
    """[now − window, now) 有動的 lot 全部重合成。回傳合了幾個 lot。"""
    es = as_backend(es)
    mes, ev = cfg["mes"], cfg.get("events") or {}
    mf = mes["fields"]
    start = now - window
    # MES 列的時間欄位：進站時間（出站晚到的用 end 欄位也查一次）
    recent = read_rows(es, {**mes, "time_field": mf["start"]}, start, now)
    if mf.get("end"):
        recent += read_rows(es, {**mes, "time_field": mf["end"]}, start, now)
    lots = _lot_ids(recent, mf["lot"])
    if not lots:
        return 0
    steps = _rows_of_lots(es, {**mes, "time_field": mf["start"]}, mf["lot"], lots, start, now)
    events = _rows_of_lots(es, {**ev, "time_field": ev["fields"]["start"]}, ev["fields"]["lot"], lots, start, now) if ev else []
    docs = P.build_docs(steps, events, cfg, now.to_pydatetime())
    if es.kind == "es":   # 來源是 ES 才有 ES 可以放 template（結果 index 在那裡）
        try:
            es.raw_req("PUT", f"/_index_template/{P.PATHS_PREFIX}", P.PATHS_TEMPLATE)
        except Exception as e:  # noqa: BLE001  沒有 manage_index_templates 權限：template 要請 ES 管理員放（configs/factory/README.md）
            print(f"  放不了 index template {P.PATHS_PREFIX}（{type(e).__name__}）；若索引還沒建，nested 查詢會失敗")
    for index, batch in docs.items():
        es.bulk_index(index, batch)
    return sum(len(b) for b in docs.values())


def register(app: typer.Typer) -> None:
    """掛成 detector 的子命令（core/plugin.Plugin.commands）。"""
    app.command("paths-merge", help="工廠：MES 進出站＋S6F11 機台事件 → 一個 lot 一筆履歷 lot-paths-*（plugins/factory/paths/build.py）")(paths_merge)


def paths_merge(config: str = typer.Option(..., help="configs/factory/paths.yaml"),
                window: str = typer.Option("1d", help="最近多久有進站／出站的 lot 重合成（預設 1d）"),
                at: str | None = typer.Option(None, help="拿某個時間點當成現在")) -> int:
    return cmd_paths_merge(load(config, PathsConfig), Namespace(config=config, window=window, at=at))


def cmd_paths_merge(cfg: dict, args) -> int:
    es = open_backend(cfg, ES)
    now = _now(args.at)
    n = run(es, cfg, now, pd.Timedelta(args.window))
    tz = cfg.get("timezone", "Asia/Taipei")
    print(f"{now.tz_convert(tz):%m-%d %H:%M}：paths-merge 最近 {args.window} 有動的 lot，合了 {n} 個")
    return 0

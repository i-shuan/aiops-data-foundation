"""ingest：讀 ML／FDC 判好的異常，轉成 aiops-anomalies（detector/ingest.py）。排程器每分鐘跑，看最近 --window（預設 10 分鐘）；
_id 由來源的識別碼算，重讀不重複。來源的原始列由 Backend 給：ES 直接查索引（sources/es_source.py），設定檔有 source: file／http 時走
sources 的列（離線跑假資料）。"""
from __future__ import annotations

import pandas as pd

from core.es_client import ES
from core.timeutil import _now
from sources import as_backend, open_backend
from sources.schema import ANOMALY_INDEX, ANOMALY_MAPPING, INGEST_FIELDS

from . import ingest as I
from .owner import write_anomalies


def read_rows(es, inp: dict, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    """來源在 [start, end) 的原始列（Backend.source_rows）。工廠的 paths-merge 也用。"""
    return as_backend(es).source_rows(inp, start, end)


def _prepare(es, cfg: dict) -> None:
    bad = I.validate(cfg)
    if bad:
        raise ValueError("ingest 設定有問題：" + "；".join(bad))
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    es.req("PUT", f"/{ANOMALY_INDEX}/_mapping", {"properties": INGEST_FIELDS})


def ingest_rows(es, cfg: dict, name: str, rows: list[dict], now: pd.Timestamp) -> tuple[int, int]:
    """api 用（POST /api/anomalies）：別人推進來的列（欄位跟那個 input 的索引裡一樣）→ 照 inputs 裡叫 name 的對照轉成異常、寫進 ⑤。
    回 (寫了幾筆, 略過幾筆)。沒有這個 input → LookupError。"""
    _prepare(es, cfg)
    inp = next((i for i in cfg["inputs"] if i.get("name") == name), None)
    if inp is None:
        raise LookupError(f"ingest.yaml 沒有叫 {name!r} 的 input（有：{', '.join(str(i.get('name')) for i in cfg['inputs'])}）")
    docs, skipped = I.convert(rows, inp, now.to_pydatetime())
    if docs:
        write_anomalies(es, docs)
    return len(docs), skipped


def run(es, cfg: dict, now: pd.Timestamp, window: pd.Timedelta) -> dict[str, tuple[int, int]]:
    """每個 input 讀 [now − window, now) 的列、轉成異常、寫進 aiops-anomalies。回傳 {來源名: (寫了幾筆, 略過幾筆)}。"""
    try:
        _prepare(es, cfg)
    except ValueError as e:
        raise SystemExit(str(e))
    start = now - window
    out = {}
    for inp in cfg["inputs"]:
        rows = read_rows(es, inp, start, now)
        docs, skipped = I.convert(rows, inp, now.to_pydatetime())
        if docs:
            write_anomalies(es, docs)
        out[inp["name"]] = (len(docs), skipped)
    return out


def cmd_ingest(cfg: dict, args) -> int:
    es = open_backend(cfg, ES)
    now = _now(args.at)
    window = pd.Timedelta(args.window)
    tz = cfg.get("timezone", "Asia/Taipei")
    got = run(es, cfg, now, window)
    for name, (n, skipped) in got.items():
        print(f"  {name}：{n} 筆異常" + (f"（略過 {skipped} 筆：沒有時間、對象或值）" if skipped else ""))
    print(f"{now.tz_convert(tz):%m-%d %H:%M}：接收 {sum(n for n, _ in got.values())} 筆（最近 {args.window}）")
    return 0

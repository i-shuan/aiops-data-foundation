"""ingest：讀 ML／FDC 判好的異常，轉成 aiops-anomalies（detect/fab_ingest/ingest.py）。排程器每分鐘跑，看最近 --window（預設 10 分鐘）；
_id 由來源的識別碼算，重讀不重複。來源的原始列由 repo.series.rows 給（ES 直接查索引）。"""
from __future__ import annotations

import pandas as pd

from common.infra.repos import Repos

import detect.fab_ingest.ingest as I
from detect.config.ingest import IngestConfig

INGEST_FIELDS = {"source": {"type": "keyword"}, "source_id": {"type": "keyword"}, "status": {"type": "keyword"}}


def _prepare(repo: Repos, ingest_config: IngestConfig) -> None:
    bad = I.validate(ingest_config)
    if bad:
        raise ValueError("ingest 設定有問題：" + "；".join(bad))
    repo.anomalies.ensure(INGEST_FIELDS)


def ingest_rows(repo: Repos, ingest_config: IngestConfig, name: str, rows: list[dict], now: pd.Timestamp) -> tuple[int, int]:
    """api 用（POST /api/anomalies）：別人推進來的列（欄位跟那個 input 的索引裡一樣）→ 照 inputs 裡叫 name 的對照轉成異常、寫進 ⑤。
    回 (寫了幾筆, 略過幾筆)。沒有這個 input → LookupError。"""
    _prepare(repo, ingest_config)
    inp = next((i for i in ingest_config.inputs if i.name == name), None)
    if inp is None:
        raise LookupError(f"ingest.yaml 沒有叫 {name!r} 的 input（有：{', '.join(i.name for i in ingest_config.inputs)}）")
    docs, skipped = I.convert(rows, inp, now.to_pydatetime())
    repo.anomalies.write(docs)
    return len(docs), skipped


def ingest_api(repo: Repos, body: dict, ingest_config: IngestConfig | None, now: pd.Timestamp | None = None) -> dict:
    """POST /api/anomalies：{"input": "ml", "rows": [...]} → 照 ingest.yaml 裡叫 input 的對照轉成異常、寫進 aiops-anomalies。
    rows 的欄位跟那個 input 的索引裡一樣（time_field、keys、value…）。回 {"input", "written", "skipped"}。
    ingest_config 是 ingest.yaml（None＝這個情境沒有 ingest → LookupError）；沒有這個 input → LookupError；body 不對 → ValueError。"""
    name, rows = body.get("input"), body.get("rows")
    if not name or not isinstance(rows, list):
        raise ValueError('要 {"input": "<ingest.yaml 的 inputs 名>", "rows": [ {...}, ... ]}')
    if not all(isinstance(r, dict) for r in rows):
        raise ValueError("rows 每一筆要是物件")
    if ingest_config is None:
        raise LookupError("這個情境沒有 ingest（排程表沒有 ingest 工作、也沒設 AIOPS_INGEST_CONFIG）")
    written, skipped = ingest_rows(repo, ingest_config, str(name), rows, now or pd.Timestamp.now(tz="UTC"))
    return {"input": name, "written": written, "skipped": skipped}


def run(repo: Repos, ingest_config: IngestConfig, now: pd.Timestamp, window: pd.Timedelta) -> dict[str, tuple[int, int]]:
    """每個 input 讀 [now − window, now) 的列、轉成異常、寫進 aiops-anomalies。回傳 {來源名: (寫了幾筆, 略過幾筆)}。"""
    try:
        _prepare(repo, ingest_config)
    except ValueError as e:
        raise SystemExit(str(e))
    start = now - window
    out = {}
    for inp in ingest_config.inputs:
        docs, skipped = I.convert(repo.series.rows(inp, start, now), inp, now.to_pydatetime())
        repo.anomalies.write(docs)
        out[inp.name] = (len(docs), skipped)
    return out

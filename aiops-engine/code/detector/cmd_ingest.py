"""ingest：讀 ML／FDC 判好的異常，轉成 aiops-anomalies（detector/ingest.py）。排程器每分鐘跑，看最近 --window（預設 10 分鐘）；
_id 由來源的識別碼算，重讀不重複。來源是 ES 時直接查索引；設定檔有 source: file／http 時走 sources 的 Backend（離線跑假資料）。"""
from __future__ import annotations

import pandas as pd

from core.es import ES
from sources import open_backend
from sources.base import is_backend
from sources.rows import ESSourceBackend
from sources import shape

from . import ingest as I
from .common import RULE_FIELDS, _now
from .es import ANOMALY_INDEX, ANOMALY_MAPPING
from .owner import write_anomalies

INGEST_FIELDS = {**RULE_FIELDS, "source": {"type": "keyword"}, "source_id": {"type": "keyword"}, "status": {"type": "keyword"}}


def _es_rows(es, inp: dict, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    tf = inp.get("time_field", "@timestamp")
    filters = [{"range": {tf: {"gte": start.isoformat(), "lt": end.isoformat()}}}]
    if inp.get("filter"):
        filters.append(inp["filter"])
    out, after = [], None
    while True:
        body = {"size": 5000, "query": {"bool": {"filter": filters}}, "sort": [{tf: "asc"}, {"_doc": "asc"}]}
        if after:
            body["search_after"] = after
        hits = es.req("POST", f"/{inp['index']}/_search?ignore_unavailable=true&allow_no_indices=true", body).get("hits", {}).get("hits", [])
        out += [{**h["_source"], "_source_doc_id": h["_id"]} for h in hits]
        if len(hits) < 5000:
            return out
        after = hits[-1]["sort"]


def _backend_rows(be, inp: dict, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    df = be.rows(inp["index"], start, end)
    df = shape.in_window(df, inp.get("time_field", "@timestamp"), start, end)
    if df.empty:
        return []
    if inp.get("filter"):
        df = df.loc[shape.mask(df, inp["filter"])]
    rows = df.drop(columns=["ts"]).to_dict("records")
    return [{k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in r.items()} for r in rows]


def read_rows(es, inp: dict, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    """來源的原始列：ES 查索引；file／http／memory 的 Backend 用它的 rows；source es ＋ store file 用裡面的 ES。"""
    if isinstance(es, ESSourceBackend):
        return _es_rows(es.es, inp, start, end)
    if is_backend(es):
        return _backend_rows(es, inp, start, end)
    return _es_rows(es, inp, start, end)


def run(es, cfg: dict, now: pd.Timestamp, window: pd.Timedelta) -> dict[str, tuple[int, int]]:
    """每個 input 讀 [now − window, now) 的列、轉成異常、寫進 aiops-anomalies。回傳 {來源名: (寫了幾筆, 略過幾筆)}。"""
    bad = I.validate(cfg)
    if bad:
        raise SystemExit("ingest 設定有問題：" + "；".join(bad))
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    es.req("PUT", f"/{ANOMALY_INDEX}/_mapping", {"properties": INGEST_FIELDS})
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

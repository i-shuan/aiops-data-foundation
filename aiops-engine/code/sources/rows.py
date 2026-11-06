"""原始列的來源：memory（程式直接給 DataFrame）、file（CSV／Parquet／NDJSON／JSON）、http（GET 回 JSON）。ES 在 es_source.py。
三種都只負責「把某個 index 在某段時間的原始列拿出來」，整形交給 shape.py；ES 以外的來源不在這裡做 aggregation。

index 的對應：設定裡的 key 是 series.yaml 寫的 index（可以含 *），拿序列的 index 來找：完全一樣優先，再用 glob 比對。
"""
from __future__ import annotations

import fnmatch
import json
import os
from abc import abstractmethod

import pandas as pd
import requests

from core.incident import INCIDENT_INDEX
from . import shape
from .base import Backend, Store
from .stores import MemoryStore


def pick(mapping: dict, index: str, what: str):
    """mapping 的 key 是 index pattern；index 可以是逗號分隔的好幾個。回傳符合的 value 清單。"""
    out = []
    for one in str(index).split(","):
        one = one.strip()
        if one in mapping:
            out.append(mapping[one])
            continue
        hit = [v for k, v in mapping.items() if fnmatch.fnmatchcase(one, k) or fnmatch.fnmatchcase(k, one)]
        if not hit:
            raise KeyError(f"{what} 沒有 index {one!r} 的資料（有：{list(mapping)}）")
        out += hit
    return out


class RowsBackend(Backend):
    """給「原始列」的來源共用：rows(index, start, end) 由子類別提供。"""

    def __init__(self, store: Store | None = None, fmt: str = "events", spans: list[dict] | None = None):
        if fmt not in ("events", "series"):
            raise ValueError(f"format 只能是 events 或 series，不是 {fmt!r}")
        self.store = store or MemoryStore()
        self.fmt = fmt
        self._spans = spans or []

    @abstractmethod
    def rows(self, index: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame: ...

    def series(self, spec, start, end, interval):
        return shape.series_from_rows(self.rows(spec["index"], start, end), spec, start, end, interval, self.fmt)

    def alive_times(self, spec, start, end, interval):
        rows = self.rows(spec["index"], start, end)
        tf = "ts" if self.fmt == "series" and "ts" in rows.columns else spec.get("time_field", "@timestamp")
        return shape.alive_from_rows(rows, tf, start, end, interval)

    def points(self, rule, start, end):
        return shape.points_from_rows(self.rows(rule["index"], start, end), rule, start, end)

    def spans(self, index, trace_ids, start, end):
        want = set(trace_ids)
        return [s for s in self._spans if not want or s.get("trace_id") in want]

    def source_rows(self, inp, start, end):
        df = shape.in_window(self.rows(inp["index"], start, end), inp.get("time_field", "@timestamp"), start, end)
        if df.empty:
            return []
        if inp.get("filter"):
            df = df.loc[shape.mask(df, inp["filter"])]
        rows = df.drop(columns=["ts"]).to_dict("records")
        return [{k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in r.items()} for r in rows]


class MemoryBackend(RowsBackend):
    """程式直接呼叫：frames = {index pattern: DataFrame}。
        be = MemoryBackend({"metrics-apm.transaction.1m-*": df}, fmt="events")
        detector.cmd_baseline.cmd_train(cfg, args) 之類的指令把 be 當 es 用；或自己呼叫 be.series(spec, …) 餵 models/。"""
    kind = "memory"

    def __init__(self, frames: dict[str, pd.DataFrame], fmt: str = "events", store: Store | None = None,
                 spans: list[dict] | None = None, incidents: list[tuple[str, dict]] | None = None):
        super().__init__(store, fmt, spans)
        self.frames = dict(frames)
        if incidents:
            self.store.bulk_index(INCIDENT_INDEX, incidents)

    def rows(self, index, start, end):
        parts = pick(self.frames, index, "memory 來源")
        return parts[0] if len(parts) == 1 else pd.concat(parts, ignore_index=True)


def load_file(path: str) -> pd.DataFrame:
    """副檔名決定格式：.csv、.parquet、.ndjson／.jsonl（每行一個 JSON）、.json（陣列或 {"rows": […]}）。
    JSON 的巢狀物件攤成點號欄位（service.name）；CSV、Parquet 的欄位名本來就要是點號路徑。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return pd.read_csv(path)
    if ext == ".parquet":
        return pd.read_parquet(path)
    if ext in (".ndjson", ".jsonl"):
        with open(path, encoding="utf-8") as f:
            return shape.rows_from_records([json.loads(l) for l in f if l.strip()])
    if ext == ".json":
        with open(path, encoding="utf-8") as f:
            return shape.rows_from_records(json.load(f))
    raise ValueError(f"不認得的檔案格式 {path}（支援 .csv、.parquet、.ndjson、.jsonl、.json）")


class FileBackend(RowsBackend):
    """設定檔：
        source:
          kind: file
          format: events                      # 或 series
          files: {metrics-apm.transaction.1m-*: data/tx.csv, metrics-apm.service_destination.1m-*: data/dest.parquet}
          spans: data/spans.ndjson            # 選填：core/traces 格式的 span（M4）
    每個檔讀一次就留在記憶體。"""
    kind = "file"

    def __init__(self, files: dict[str, str], fmt: str = "events", store: Store | None = None, spans_path: str | None = None):
        spans = load_file(spans_path).to_dict("records") if spans_path else None
        super().__init__(store, fmt, spans)
        self.files = dict(files or {})
        self._cache: dict[str, pd.DataFrame] = {}

    def _load(self, path: str) -> pd.DataFrame:
        if path not in self._cache:
            self._cache[path] = load_file(path)
        return self._cache[path]

    def rows(self, index, start, end):
        parts = [self._load(p) for p in pick(self.files, index, "source.files")]
        return parts[0] if len(parts) == 1 else pd.concat(parts, ignore_index=True)


class HttpBackend(RowsBackend):
    """設定檔：
        source:
          kind: http
          format: events
          url: https://api.example.internal/rows?index={index}&start={start}&end={end}   # 回 JSON 陣列或 {"rows": […]}
          urls: {metrics-apm.transaction.1m-*: https://…}                                  # 或每個 index 一個網址（同樣可帶 {start}、{end}）
          headers: {Authorization: Bearer …}
          timeout: 60
    每次查詢都打一次（detect 每分鐘跑，對方要撐得住；要快取請在對方那邊做）。"""
    kind = "http"

    def __init__(self, url: str | None = None, urls: dict[str, str] | None = None, fmt: str = "events",
                 store: Store | None = None, headers: dict | None = None, timeout: float = 60, fetch=None):
        super().__init__(store, fmt)
        if not url and not urls:
            raise ValueError("source.kind http 要給 url 或 urls")
        self.url, self.urls, self.headers, self.timeout = url, dict(urls or {}), headers or {}, timeout
        self._fetch = fetch or self._http

    def _http(self, url: str):
        r = requests.get(url, headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def rows(self, index, start, end):
        targets = pick(self.urls, index, "source.urls") if self.urls else [self.url]
        frames = [shape.rows_from_records(self._fetch(t.format(index=index, start=start.isoformat(), end=end.isoformat())))
                  for t in targets]
        return frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)

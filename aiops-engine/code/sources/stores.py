"""結果存放（Store）的三種實作：memory（程式呼叫、測試）、file（一個資料夾，每個 index 一個 NDJSON）、es（現在的 ES）。
索引名跟 ES 一樣（aiops-baseline、aiops-anomalies、aiops-tuning、aiops-alerts、aiops-incidents），文件格式也一樣，
所以同一批結果搬到 ES 不用轉。"""
from __future__ import annotations

import json
import os

import pandas as pd

from .base import Store

BASELINE_INDEX, ANOMALY_INDEX, TUNING_INDEX, INCIDENT_INDEX = "aiops-baseline", "aiops-anomalies", "aiops-tuning", "aiops-incidents"
# 跟 detector/es.py 的 BASELINE_COLUMNS、_RENAMED 一樣（那邊 import 這裡會繞圈，所以各寫一份；test_sources_backend 盯著兩邊一致）
BASELINE_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution", "baseline_method", "baseline_source", "trained_at")
_RENAMED = {"baseline_method": "method"}


class MemoryStore(Store):
    kind = "memory"

    def __init__(self):
        self.data: dict[str, dict[str, dict]] = {}

    def ensure_index(self, name: str, body: dict) -> None:
        self.data.setdefault(name, {})

    def bulk_index(self, index: str, docs) -> None:
        d = self.data.setdefault(index, {})
        for _id, doc in docs:
            d[_id] = json.loads(json.dumps(doc, default=str))   # 跟 ES 一樣：存的是 JSON，Timestamp 變字串
        self._flush(index)

    def _flush(self, index: str) -> None:
        pass

    def docs(self, index: str) -> list[tuple[str, dict]]:
        return list(self.data.get(index, {}).items())

    def load_baseline(self, series: str, by: list[str]) -> pd.DataFrame:
        rows = [{**{f: s.get("keys", {}).get(f) for f in by}, **{k: s.get(_RENAMED.get(k, k)) for k in BASELINE_COLUMNS}}
                for _, s in self.docs(BASELINE_INDEX)
                if s.get("series") == series and not str(s.get("baseline_source") or "").startswith("group:")]
        return pd.DataFrame(rows, columns=by + list(BASELINE_COLUMNS))

    def load_group_baselines(self, series: str) -> pd.DataFrame:
        rows = [{"group": s["baseline_source"][len("group:"):], **{k: s.get(k) for k in ("dow", "hour", "level", "median", "mad", "resolution")}}
                for _, s in self.docs(BASELINE_INDEX)
                if s.get("series") == series and str(s.get("baseline_source") or "").startswith("group:")]
        return pd.DataFrame(rows, columns=["group", "dow", "hour", "level", "median", "mad", "resolution"])

    def save_baseline(self, series: str, docs, trained_at: str, had_data: bool) -> None:
        self.bulk_index(BASELINE_INDEX, docs)
        if docs or had_data:
            d = self.data.get(BASELINE_INDEX, {})
            for _id in [i for i, s in d.items() if s.get("series") == series and str(s.get("trained_at")) < trained_at]:
                del d[_id]
            self._flush(BASELINE_INDEX)

    def anomaly_status(self, ids: list[str]) -> dict[str, dict]:
        d = self.data.get(ANOMALY_INDEX, {})
        return {i: {"status": d[i].get("status"), "streak": d[i].get("streak")} for i in ids if i in d and d[i].get("status") == "anomaly"}

    def adjustments(self) -> list[dict]:
        out = [{"id": _id, **s} for _id, s in self.docs(TUNING_INDEX)]
        return sorted(out, key=lambda a: str(a.get("at") or ""))

    def write_anomalies(self, docs) -> None:
        if not docs:
            return
        from core.version import ENGINE_VERSION
        for _, d in docs:
            d.setdefault("dept", None)
            d.setdefault("group", "unassigned")
            d["engine_version"] = ENGINE_VERSION
        self.bulk_index(ANOMALY_INDEX, docs)

    def incidents(self, statuses, exclude_id=None) -> list[tuple[str, dict]]:
        return [(i, s) for i, s in self.docs(INCIDENT_INDEX) if s.get("status") in statuses and i != exclude_id]


class FileStore(MemoryStore):
    """一個資料夾，每個 index 一個 <index>.ndjson（每行 {"_id": …, …文件}）。開啟時全部讀進記憶體，每次寫入整個 index 重寫。
    給離線跑：train 寫的 baseline 下一次 detect 讀得到。幾萬筆以內夠用；再大就該用 ES。"""
    kind = "file"

    def __init__(self, dir: str):
        super().__init__()
        self.dir = dir
        os.makedirs(dir, exist_ok=True)
        for fn in os.listdir(dir):
            if fn.endswith(".ndjson"):
                with open(os.path.join(dir, fn), encoding="utf-8") as f:
                    self.data[fn[:-len(".ndjson")]] = {d.pop("_id"): d for d in (json.loads(l) for l in f if l.strip())}

    def _flush(self, index: str) -> None:
        with open(os.path.join(self.dir, f"{index}.ndjson"), "w", encoding="utf-8") as f:
            for _id, doc in self.data.get(index, {}).items():
                f.write(json.dumps({"_id": _id, **doc}, ensure_ascii=False, default=str) + "\n")


class ESStore(Store):
    """結果寫進 ES（原始資料不在 ES、結果要在 ES 時用：source: file ＋ store: es）。做法跟 detector 各指令直接打 ES 一樣。"""
    kind = "es"

    def __init__(self, es):
        self.es = es

    def ensure_index(self, name, body):
        self.es.ensure_index(name, body)

    def bulk_index(self, index, docs):
        self.es.bulk_index(index, docs)

    def req(self, method, path, body=None, ok=None):
        return self.es.req(method, path, body, **({"ok": ok} if ok else {}))

    def load_baseline(self, series, by):
        from detector.es import load_baseline
        return load_baseline(self.es, series, by)

    def load_group_baselines(self, series):
        from detector.es import load_group_baselines
        return load_group_baselines(self.es, series)

    def save_baseline(self, series, docs, trained_at, had_data):
        self.es.bulk_index(BASELINE_INDEX, docs)
        if docs or had_data:
            self.es.req("POST", f"/{BASELINE_INDEX}/_delete_by_query?conflicts=proceed", {"query": {"bool": {"filter": [
                {"term": {"series": series}}, {"range": {"trained_at": {"lt": trained_at}}}]}}})

    def anomaly_status(self, ids):
        got = self.es.req("POST", f"/{ANOMALY_INDEX}/_mget?_source_includes=status,streak", {"ids": ids}).get("docs", [])
        return {g["_id"]: g["_source"] for g in got if g.get("found") and g["_source"].get("status") == "anomaly"}

    def adjustments(self):
        hits = self.es.req("POST", f"/{TUNING_INDEX}/_search", {"size": 10000, "sort": [{"at": {"order": "asc", "unmapped_type": "date"}}]},
                           ok=(200, 404)).get("hits", {}).get("hits", [])
        return [{"id": h["_id"], **h["_source"]} for h in hits]

    def write_anomalies(self, docs):
        from detector.owner import write_anomalies
        write_anomalies(self.es, docs)

    def incidents(self, statuses, exclude_id=None):
        q = {"bool": {"filter": [{"terms": {"status": list(statuses)}}]}}
        if exclude_id:
            q["bool"]["must_not"] = [{"ids": {"values": [exclude_id]}}]
        hits = self.es.search(INCIDENT_INDEX, {"size": 1000, "query": q}).get("hits", {}).get("hits", [])
        return [(h["_id"], h["_source"]) for h in hits]


def open_store(cfg: dict | None, es_factory, default: str = "memory") -> Store:
    """設定檔的 store: {kind: memory | file | es, dir: …}。"""
    cfg = cfg or {}
    kind = cfg.get("kind", default)
    if kind == "memory":
        return MemoryStore()
    if kind == "file":
        return FileStore(cfg.get("dir", ".aiops-store"))
    if kind == "es":
        return ESStore(es_factory())
    raise ValueError(f"store.kind 只能是 memory、file、es，不是 {kind!r}")

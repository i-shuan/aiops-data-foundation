"""結果存放（Store）的三種實作：memory（程式呼叫、測試）、file（一個資料夾，每個 index 一個 NDJSON）、es（現在的 ES）。
索引名跟 ES 一樣（aiops-baseline、aiops-anomalies、aiops-tuning、aiops-alerts、aiops-incidents），文件格式也一樣，
所以同一批結果搬到 ES 不用轉。"""
from __future__ import annotations

import json
import os

import pandas as pd

from core.anomaly import ANOMALY_INDEX
from core.incident import INCIDENT_INDEX, Fingerprint
from core.owner import load_owners, owner_of
from core.tuning import TUNING_INDEX
from .base import Store

BASELINE_INDEX = "aiops-baseline"
# baseline 讀回來的欄位（detector/es.py 從這裡 import）。method 存成 baseline_method：序列的 keys 也可能有 method（HTTP method），會撞名
BASELINE_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution", "baseline_method", "baseline_source", "trained_at")
_RENAMED = {"baseline_method": "method"}
_GROUP_COLUMNS = ("dow", "hour", "level", "median", "mad", "resolution")


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
        rows = [{"group": s["baseline_source"][len("group:"):], **{k: s.get(k) for k in _GROUP_COLUMNS}}
                for _, s in self.docs(BASELINE_INDEX)
                if s.get("series") == series and str(s.get("baseline_source") or "").startswith("group:")]
        return pd.DataFrame(rows, columns=["group", *_GROUP_COLUMNS])

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
        """檔案、記憶體來源沒有 dept／group 的資料可查：全部 unassigned。"""
        if not docs:
            return
        for _, d in docs:
            d.setdefault("dept", None)
            d.setdefault("group", "unassigned")
        self.bulk_index(ANOMALY_INDEX, docs)

    def incidents(self, statuses, exclude_id=None, having=()) -> list[tuple[str, dict]]:
        return [(i, s) for i, s in self.docs(INCIDENT_INDEX)
                if s.get("status") in statuses and i != exclude_id and all(s.get(f) for f in having)]


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
    """結果在 ES（現況；也給 source: file ＋ store: es）。es 是 core.es.ES（或測試裡只有 req／search／bulk_index 的假 ES）。"""
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
        """這條序列自己學出來的 baseline（不含借給別人用的組 baseline）。"""
        rows, after = [], None
        while True:
            body = {"size": 10000, "query": {"bool": {"filter": [{"term": {"series": series}}],
                                                      "must_not": [{"prefix": {"baseline_source": "group:"}}]}},
                    "sort": [{"_doc": "asc"}]}
            if after:
                body["search_after"] = after
            hits = self.es.req("POST", f"/{BASELINE_INDEX}/_search?ignore_unavailable=true", body).get("hits", {}).get("hits", [])
            for h in hits:
                s = h["_source"]
                rows.append({**{f: s["keys"].get(f) for f in by}, **{k: s.get(_RENAMED.get(k, k)) for k in BASELINE_COLUMNS}})
            if len(hits) < 10000:
                break
            after = hits[-1]["sort"]
        return pd.DataFrame(rows, columns=by + list(BASELINE_COLUMNS))

    def load_group_baselines(self, series):
        """train 時從成員算出來的組 baseline：group + dow、hour、level、median、mad、resolution。"""
        body = {"size": 10000, "query": {"bool": {"filter": [{"term": {"series": series}},
                                                             {"prefix": {"baseline_source": "group:"}}]}}}
        hits = self.es.req("POST", f"/{BASELINE_INDEX}/_search?ignore_unavailable=true", body).get("hits", {}).get("hits", [])
        rows = [{"group": h["_source"]["baseline_source"][len("group:"):], **{k: h["_source"].get(k) for k in _GROUP_COLUMNS}} for h in hits]
        return pd.DataFrame(rows, columns=["group", *_GROUP_COLUMNS])

    def save_baseline(self, series, docs, trained_at, had_data):
        self.es.bulk_index(BASELINE_INDEX, docs)
        if docs or had_data:   # ES 查不到任何資料（例如停機）就不刪，保留舊的
            self.es.req("POST", f"/{BASELINE_INDEX}/_delete_by_query?conflicts=proceed", {"query": {"bool": {"filter": [
                {"term": {"series": series}}, {"range": {"trained_at": {"lt": trained_at}}}]}}})

    def anomaly_status(self, ids):
        got = self.es.req("POST", f"/{ANOMALY_INDEX}/_mget?_source_includes=status,streak", {"ids": ids}).get("docs", [])
        return {g["_id"]: g["_source"] for g in got if g.get("found") and g["_source"].get("status") == "anomaly"}

    def adjustments(self):
        # 讀不到（index 關閉、沒權限、ES 逾時）不整個失敗：印出來，這一輪只用設定檔的。index 不存在是正常的（還沒人調過）
        try:
            hits = self.es.req("POST", f"/{TUNING_INDEX}/_search", {"size": 10000, "sort": [{"at": {"order": "asc", "unmapped_type": "date"}}]},
                               ok=(200, 404)).get("hits", {}).get("hits", [])
        except Exception as e:  # noqa: BLE001  RuntimeError（ES 回錯誤）或 requests 的連線、逾時錯誤
            print(f"  ⚠ 讀不到 {TUNING_INDEX}（{str(e)[:120]}），這一輪不套用裡面的調整，只用設定檔的")
            hits = []
        return [{"id": h["_id"], **h["_source"]} for h in hits]

    def write_anomalies(self, docs):
        """每筆依 keys 裡的 namespace／服務對回 dept、group（core/owner.py）。"""
        if not docs:
            return
        owners = load_owners(self.es)
        for _, d in docs:
            d["dept"], d["group"] = owner_of(d.get("keys"), owners)
        self.es.bulk_index(ANOMALY_INDEX, docs)

    def incidents(self, statuses, exclude_id=None, having=()):
        q = {"bool": {"filter": [{"terms": {"status": list(statuses)}}, *({"exists": {"field": f}} for f in having)]}}
        if exclude_id:
            q["bool"]["must_not"] = [{"ids": {"values": [exclude_id]}}]
        hits = self.es.search(INCIDENT_INDEX, {"size": 1000, "query": q}).get("hits", {}).get("hits", [])
        return [(h["_id"], h["_source"]) for h in hits]

    def similar_incidents(self, fp, statuses, exclude_id=None, size=200):
        """ES 用指紋的 bool／terms 先篩出前 size 件（Fingerprint.es_query），分數還是 investigator.similar.rank 自己算。"""
        hits = self.es.search(INCIDENT_INDEX, Fingerprint.of(fp).es_query(exclude_id, size, statuses)).get("hits", {}).get("hits", [])
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

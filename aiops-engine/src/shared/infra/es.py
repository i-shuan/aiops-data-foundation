"""ES 的共用部分：連線與查詢小工具（原 connection）、寫進 ES 的文件的 pydantic 基底 Doc（原 document）、repository 基底 Repository（原 repository）。
一個 repository 管一個索引（或一組同格式的索引）的讀寫，索引名與 ES 的查詢只出現在各功能的 store/；
es 是這裡的 ES；測試裡只要有 req／search／bulk_index／ensure_index 的假 ES 也行（tests/fakes.py 另有不連 ES 的記憶體版）。

連線用環境變數 ES_URL、ES_API_KEY（細節見 class ES）。
"""
from __future__ import annotations

from shared.config.settings import settings

import hashlib
import json
from datetime import datetime

import requests
from pydantic import BaseModel, ConfigDict


class ES:
    def __init__(self, url: str | None = None):
        cfg = settings()
        self.url = (url or cfg.require_es_url()).rstrip("/")
        self.s = requests.Session()
        if cfg.es_api_key:
            self.s.headers["Authorization"] = f"ApiKey {cfg.es_api_key}"
        elif cfg.es_auth:
            self.s.auth = tuple(cfg.es_auth.split(":", 1))
        if cfg.es_ca:
            self.s.verify = cfg.es_ca

    def req(self, method: str, path: str, body=None, ok=(200, 201)):
        r = self.s.request(method, self.url + path, json=body, timeout=120)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    def search(self, index: str, body: dict) -> dict:
        r = self.s.post(f"{self.url}/{index}/_search?ignore_unavailable=true&allow_no_indices=true", json=body, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"查詢 {index} 失敗：{r.status_code} {r.text[:500]}")
        return r.json()

    def ensure_index(self, name: str, body: dict):
        if self.s.head(f"{self.url}/{name}", timeout=30).status_code == 404:
            r = self.s.put(f"{self.url}/{name}", json=body, timeout=120)
            if r.status_code in (200, 201):
                return
            # 排程器讓 rules、detect、alert 同一秒起跑，第一次都看到 404、搶著建同一個 index：輸的那個收到
            # resource_already_exists_exception，不算錯，往下補 mapping 就好（實測一隻 pod 的第一分鐘 rules 因此失敗一次）
            if not (r.status_code == 400 and "resource_already_exists_exception" in r.text):
                raise RuntimeError(f"PUT /{name} → {r.status_code}: {r.text[:500]}")
        if body.get("mappings", {}).get("properties"):
            # 已經有的 index 補上新加的欄位（只能加，不能改型別）
            self.req("PUT", f"/{name}/_mapping", {"properties": body["mappings"]["properties"]})

    def bulk_index(self, index: str, docs: list[tuple[str, dict]], chunk: int = 2000):
        """docs：(_id, 文件)。同一個 _id 再寫一次會覆蓋，重跑不會重複。"""
        for i in range(0, len(docs), chunk):
            lines = []
            for _id, doc in docs[i:i + chunk]:
                lines.append(json.dumps({"index": {"_index": index, "_id": _id}}))
                lines.append(json.dumps(doc, default=str))
            r = self.s.post(f"{self.url}/_bulk?refresh=wait_for", data="\n".join(lines) + "\n",
                            headers={"Content-Type": "application/x-ndjson"}, timeout=300)
            body = r.json()
            if r.status_code != 200 or body.get("errors"):
                bad = [it for it in body.get("items", []) if it["index"].get("error")][:3]
                raise RuntimeError(f"bulk 寫入 {index} 失敗：{r.status_code} {bad or r.text[:500]}")


def doc_id(*parts) -> str:
    """同樣的內容算出同一個 _id（寫入不重複）。只是當識別碼、不是安全用途，所以 sha1 夠用；換演算法會讓既有資料的 _id 全變，不要換。"""
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode(), usedforsecurity=False).hexdigest()


def get_field(d, dotted: str):
    """ES 的 _source 裡，同一個欄位可能是巢狀（{"db": {"system": ...}}）、攤平（{"db.system": ...}），或兩者混用。"""
    if not isinstance(d, dict):
        return None
    if dotted in d:
        return d[dotted]
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        head = ".".join(parts[:i])
        if head in d:
            v = get_field(d[head], ".".join(parts[i:]))
            if v is not None:
                return v
    return None


def time_range(start: datetime, end: datetime, field: str = "@timestamp") -> dict:
    return {"range": {field: {"gte": start.isoformat(), "lt": end.isoformat()}}}


class Doc(BaseModel):
    """寫進 ES 的文件共同的做法：建構時多出來的鍵就是錯（打錯欄位名立刻擋）；to_doc 不寫 None；from_doc 讀舊文件寬鬆。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)

    @classmethod
    def from_doc(cls, d: dict | None):
        """ES 的 _source → 物件。不認得的鍵忽略（舊文件、別的程式多寫的），缺的給預設值。"""
        d = d or {}
        names = {f.alias or n for n, f in cls.model_fields.items()} | set(cls.model_fields)
        return cls.model_validate({k: v for k, v in d.items() if k in names})

    def to_doc(self) -> dict:
        """寫進 ES 的 dict：欄位名用 ES 的（@timestamp）；只寫建構時有給的欄位（沒碰的預設值不寫，匯入的事故不會多出 anomaly_ids: []），
        給了但是 None 的也不寫；給了空陣列照寫。from_doc 讀進來再 to_doc 會跟原文件一樣。"""
        d = self.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}


_SEARCH = "_search?ignore_unavailable=true&allow_no_indices=true"


class Repository:
    INDEX: str = ""
    MAPPING: dict = {}

    def __init__(self, es, scenario=None):
        self.es, self.scenario = es, scenario

    # ── 索引 ──
    def ensure(self, index: str | None = None, mapping: dict | None = None) -> None:
        """沒有就建、有就補 mapping 裡新加的欄位（connection.ES.ensure_index）。"""
        self.es.ensure_index(index or self.INDEX, mapping or self.MAPPING)

    def add_fields(self, properties: dict, index: str | None = None) -> None:
        """既有的索引補欄位（dynamic false 的索引不補查不到）。"""
        self.es.req("PUT", f"/{index or self.INDEX}/_mapping", {"properties": properties})

    # ── 讀 ──
    def _search(self, body: dict, index: str | None = None) -> dict:
        return self.es.search(index or self.INDEX, body)

    def _hits(self, body: dict, index: str | None = None) -> list[dict]:
        return self._search(body, index).get("hits", {}).get("hits", [])

    def _scan(self, body: dict, index: str | None = None, size: int = 5000, sort: list | None = None) -> list[dict]:
        """search_after 翻頁到底，回全部 hit。sort 預設 @timestamp、_doc。"""
        body = {**body, "size": size, "sort": sort or [{"@timestamp": "asc"}, {"_doc": "asc"}]}
        out, after = [], None
        while True:
            if after:
                body["search_after"] = after
            hits = self._hits(body, index)
            out += hits
            if len(hits) < size:
                return out
            after = hits[-1]["sort"]

    def _composite(self, body: dict, path: tuple[str, ...], index: str | None = None) -> list[dict]:
        """composite aggregation 翻頁到底，回全部 bucket。path：body["aggs"] 裡到 composite 那一層的 key（例如 ("g",)、("n", "e")）。"""
        def node(d):
            for k in path:
                d = d["aggs"][k]
            return d
        out, after = [], None
        while True:
            if after:
                node(body)["composite"]["after"] = after
            agg = self._search(body, index).get("aggregations", {})
            for k in path:
                agg = agg.get(k, {})
            out += agg.get("buckets", [])
            after = agg.get("after_key")
            if not after or not agg.get("buckets"):
                return out

    def _get(self, _id: str, index: str | None = None) -> dict | None:
        got = self.es.req("GET", f"/{index or self.INDEX}/_doc/{_id}", ok=(200, 404))
        return got.get("_source") if got.get("found") else None

    def _mget(self, ids: list[str], fields: list[str] | None = None, index: str | None = None, chunk: int = 1000) -> dict[str, dict]:
        """{_id: _source}，只回找得到的。"""
        out = {}
        q = f"?_source_includes={','.join(fields)}" if fields else ""
        for i in range(0, len(ids), chunk):
            got = self.es.req("POST", f"/{index or self.INDEX}/_mget{q}", {"ids": list(ids[i:i + chunk])}).get("docs", [])
            out.update({g["_id"]: g["_source"] for g in got if g.get("found")})
        return out

    # ── 寫 ──
    def _put(self, _id: str, doc: dict, index: str | None = None) -> None:
        self.es.req("PUT", f"/{index or self.INDEX}/_doc/{_id}?refresh=true", doc)

    def _patch(self, _id: str, doc: dict, upsert: dict | None = None, index: str | None = None) -> None:
        body = {"doc": doc, **({"upsert": upsert} if upsert is not None else {})}
        self.es.req("POST", f"/{index or self.INDEX}/_update/{_id}?refresh=true", body)

    def _delete(self, _id: str, index: str | None = None) -> None:
        self.es.req("DELETE", f"/{index or self.INDEX}/_doc/{_id}?refresh=true", ok=(200, 404))

    def bulk(self, docs: list[tuple[str, dict]], index: str | None = None) -> None:
        """(_id, 文件)；同 _id 覆蓋。"""
        if docs:
            self.es.bulk_index(index or self.INDEX, docs)

    def _bulk_update(self, lines: list[dict], index: str | None = None, ignore_missing: bool = False, what: str = "",
                     counted: tuple[str, ...] = ("created", "updated")) -> int:
        """_bulk 的 update 動作（partial doc 或 script）：lines 每筆是 {"_id": …, **動作內容}。回 result 在 counted 的筆數。"""
        index = index or self.INDEX
        nd = []
        for ln in lines:
            ln = dict(ln)
            nd.append(json.dumps({"update": {"_index": index, "_id": ln.pop("_id")}}))
            nd.append(json.dumps(ln, default=str))
        r = self.es.s.post(f"{self.es.url}/_bulk?refresh=wait_for", data="\n".join(nd) + "\n",
                           headers={"Content-Type": "application/x-ndjson"}, timeout=300)
        body = r.json()
        items = body.get("items", [])
        bad = [it for it in items if it["update"].get("error")
               and not (ignore_missing and it["update"]["error"].get("type") == "document_missing_exception")]
        if r.status_code != 200 or bad:
            raise RuntimeError(f"寫入 {index}{what} 失敗：{r.status_code} {bad[:3] or r.text[:500]}")
        return sum(1 for it in items if it["update"].get("result") in counted)

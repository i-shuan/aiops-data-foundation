"""repository 的共用部分：一個 repository 管一個索引（或一組同格式的索引）的讀寫，索引名與 ES 的查詢只出現在這一層。

es 是 shared.connection.ES；測試裡只要有 req／search／bulk_index／ensure_index 的假 ES 也行（tests/fakes.py 另有不連 ES 的記憶體版）。
"""
from __future__ import annotations

import json

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

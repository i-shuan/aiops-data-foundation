"""跟 ES 溝通的共用部分：連線、查詢、寫入。aiops_baseline、aiops_rca 都用這一份。

連線用環境變數：
  ES_URL                       例如 https://es.example.internal:9200
  ES_API_KEY 或 ES_AUTH        API key 的 encoded，或 帳號:密碼（elasticsearch-setup.sh 發的「AIOps baseline key」：
                               能讀 log、metrics、trace，只能寫 aiops-*）
  ES_CA（選填）                ES 憑證的 CA 檔；不給就用系統的
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

import requests


class ES:
    def __init__(self, url: str | None = None):
        self.url = (url or os.environ["ES_URL"]).rstrip("/")
        self.s = requests.Session()
        if os.environ.get("ES_API_KEY"):
            self.s.headers["Authorization"] = f"ApiKey {os.environ['ES_API_KEY']}"
        elif os.environ.get("ES_AUTH"):
            self.s.auth = tuple(os.environ["ES_AUTH"].split(":", 1))
        if os.environ.get("ES_CA"):
            self.s.verify = os.environ["ES_CA"]

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
            self.req("PUT", f"/{name}", body)
        elif body.get("mappings", {}).get("properties"):
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
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


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

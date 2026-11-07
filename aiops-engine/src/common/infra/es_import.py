"""一次性：把以前存在 ES 的 aiops-*（engine 自己的資料）搬進 PostgreSQL（docs/pdca/meta/2026-11-postgres-store.md）。

  python3 -m main detector db import-es [--only alerts,incidents] [--dry-run]

一個索引對一張表（LEGACY）；文件原樣搬（_id 當 id、_source 當 doc），同 id 再搬一次覆蓋，所以中斷了重跑就好。
ES 那邊不刪：搬完、對過筆數、Grafana 也改讀 PostgreSQL 之後，再由人手動刪索引（elasticsearch/kibana-dev-tools.txt）。
"""
from __future__ import annotations

# 以前的 ES 索引 → Repos 的名字（PostgreSQL 的表）。aiops-mapping 不在 Repos 裡，另外處理
LEGACY = {
    "aiops-anomalies": "anomalies", "aiops-baseline": "baseline", "aiops-alerts": "alerts", "aiops-incidents": "incidents",
    "aiops-feedback": "feedback", "aiops-tuning": "tuning", "aiops-state": "state", "aiops-chronic": "chronic",
    "aiops-mapping": "mapping",
}
PAGE = 2000


def scan(es, index: str):
    """一個索引的全部文件 [(id, 文件)]，一頁一頁讀（search_after）；索引不存在回空。"""
    after = None
    while True:
        body = {"size": PAGE, "sort": [{"_doc": "asc"}], "query": {"match_all": {}}, **({"search_after": after} if after else {})}
        res = es.req("POST", f"/{index}/_search", body, ok=(200, 404))
        hits = res.get("hits", {}).get("hits", [])
        if not hits:
            return
        yield [(h["_id"], h["_source"]) for h in hits]
        if len(hits) < PAGE:
            return
        after = hits[-1]["sort"]


def run(es, targets: dict, only: set[str] | None = None, dry_run: bool = False) -> list[tuple[str, str, int]]:
    """targets：{Repos 的名字: repository}。回 [(索引, 表, 筆數)]。"""
    out = []
    for index, name in LEGACY.items():
        if only and name not in only:
            continue
        repo = targets[name]
        n = 0
        for page in scan(es, index):
            n += len(page)
            if not dry_run:
                repo.bulk(page)
        out.append((index, repo.TABLE, n))
    return out

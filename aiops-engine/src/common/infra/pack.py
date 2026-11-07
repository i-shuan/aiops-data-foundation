"""過期資料的壓縮（docs/pdca/meta/2026-11-prune-pack.md）：超過保留天數的資料，一天打包成一筆 gzip，放在 PostgreSQL 的 aiops_packed，原本的細筆刪掉。

    aiops_packed 一筆 = 某張表某一天的一批資料：id = <表>:<日>:<這批第一筆的 id>、ts = 那一天、
    doc = {"table", "day", "n"（幾筆）, "data"（base64 的 gzip；解開是一行一筆的 {"id","ts","doc"}）}

不寫檔、不用資料夾：壓縮的資料跟其他資料一起在資料庫裡，備份、權限都跟著資料庫。大約壓成 1/6～1/8（base64 多 1/3）。
一天資料很多時分成好幾筆（prune 一批一批處理）；同一天的各批 id 不同。讀回用 `detector db restore`：照原本的 id、ts 寫回原表，同 id 覆蓋。
"""
from __future__ import annotations

import base64
import gzip
import json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from common.clock.parse import stored_time
from common.infra.pg import PgRepository

PACKED_TABLE = "aiops_packed"


def pack(rows: list[dict]) -> str:
    """rows：[{id, ts, doc}] → base64 的 gzip（一行一筆）。"""
    lines = [json.dumps({"id": r["id"], "ts": r["ts"].isoformat() if isinstance(r.get("ts"), datetime) else None, "doc": r["doc"]},
                        ensure_ascii=False, default=str) for r in rows]
    return base64.b64encode(gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))).decode("ascii")


def unpack(data: str) -> list[tuple[str, datetime | None, dict]]:
    out = []
    for line in gzip.decompress(base64.b64decode(data)).decode("utf-8").splitlines():
        if line:
            r = json.loads(line)
            out.append((r["id"], stored_time(r["ts"]) if r.get("ts") else None, r["doc"]))
    return out


class PackedRepository(PgRepository):
    TABLE, TIME = PACKED_TABLE, "ts"
    INDEXES = ("((doc->>'table'), ts)",)

    def add(self, table: str, rows: list[dict], tz: str = "UTC") -> int:
        """rows 依天（tz）分開打包寫進來；回幾包。"""
        zone, by_day = ZoneInfo(tz), defaultdict(list)
        for r in rows:
            by_day[r["ts"].astimezone(zone).date().isoformat() if isinstance(r.get("ts"), datetime) else "undated"].append(r)
        docs = [(f"{table}:{day}:{part[0]['id']}",
                 {"ts": f"{day}T00:00:00+00:00" if day != "undated" else None, "table": table, "day": day, "n": len(part), "data": pack(part)})
                for day, part in sorted(by_day.items())]
        self.bulk(docs)
        return len(docs)

    def packs(self, table: str | None = None, start: str | None = None, end: str | None = None) -> list[dict]:
        """要讀回的包（day 在 [start, end] 之內，YYYY-MM-DD，含頭尾）。"""
        where, params = ["TRUE"], []
        if table:
            where.append("doc->>'table' = %s")
            params.append(table)
        if start:
            where.append("doc->>'day' >= %s")
            params.append(start)
        if end:
            where.append("doc->>'day' <= %s")
            params.append(end)
        return [d for _, d in self._select(" AND ".join(where), tuple(params), order="doc->>'day', id")]

"""過期資料的壓縮（docs/pdca/meta/2026-11-prune-pack.md）：超過保留天數的資料，一天打包成一筆 gzip，放在 PostgreSQL 的 aiops_packed，原本的細筆刪掉。

    aiops_packed 一筆 = 某張表某一天的一批：id = <表>:<日>:<這批第一筆的 id>、ts = 那一天、
    doc = {"table", "day", "n"（幾筆）}（說明）、data = bytea（gzip；解開是一行一筆的 {"id","ts","doc"}）

資料放 bytea 欄（PostgreSQL 的「檔案」：二進位內容直接存，不轉 base64）。不寫檔、不用資料夾：壓縮的資料跟其他資料一起在資料庫裡，
備份、權限都跟著資料庫。大約壓成 1/8。一天資料很多時分成好幾筆（prune 一批一批處理）；同一天的各批 id 不同。
讀回用 `detector db restore`：照原本的 id、ts 寫回原表，同 id 覆蓋。
"""
from __future__ import annotations

import gzip
import json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from common.clock.parse import stored_time
from common.infra.pg import PgRepository

PACKED_TABLE = "aiops_packed"


def pack(rows: list[dict]) -> bytes:
    """rows：[{id, ts, doc}] → gzip（一行一筆）。"""
    lines = [json.dumps({"id": r["id"], "ts": r["ts"].isoformat() if isinstance(r.get("ts"), datetime) else None, "doc": r["doc"]},
                        ensure_ascii=False, default=str) for r in rows]
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))


def unpack(data: bytes) -> list[tuple[str, datetime | None, dict]]:
    out = []
    for line in gzip.decompress(bytes(data)).decode("utf-8").splitlines():
        if line:
            r = json.loads(line)
            out.append((r["id"], stored_time(r["ts"]) if r.get("ts") else None, r["doc"]))
    return out


class PackedRepository(PgRepository):
    TABLE, TIME = PACKED_TABLE, "ts"
    INDEXES = ("((doc->>'table'), ts)",)

    def ddl(self) -> list[str]:
        t = self.table
        return [f"CREATE TABLE IF NOT EXISTS {t} (id text PRIMARY KEY, ts timestamptz, doc jsonb NOT NULL, data bytea NOT NULL, "
                f"updated_at timestamptz NOT NULL DEFAULT now())",
                f"CREATE INDEX IF NOT EXISTS {t}_ts ON {t} (ts)",
                *[f"CREATE INDEX IF NOT EXISTS {t}_x{i} ON {t} {expr}" for i, expr in enumerate(self.INDEXES)]]

    def add(self, table: str, rows: list[dict], tz: str = "UTC") -> int:
        """rows 依天（tz）分開打包寫進來；回幾包。同 id 覆蓋。"""
        zone, by_day = ZoneInfo(tz), defaultdict(list)
        for r in rows:
            by_day[r["ts"].astimezone(zone).date().isoformat() if isinstance(r.get("ts"), datetime) else "undated"].append(r)
        self.ensure()
        packs = [(f"{table}:{day}:{part[0]['id']}", stored_time(f"{day}T00:00:00+00:00") if day != "undated" else None,
                  json.dumps({"table": table, "day": day, "n": len(part)}), pack(part)) for day, part in sorted(by_day.items())]
        self.db.executemany(f"INSERT INTO {self.table} (id, ts, doc, data) VALUES (%s, %s, %s::jsonb, %s) "
                            f"ON CONFLICT (id) DO UPDATE SET ts = EXCLUDED.ts, doc = EXCLUDED.doc, data = EXCLUDED.data, updated_at = now()", packs)
        return len(packs)

    def packs(self, table: str | None = None, start: str | None = None, end: str | None = None, data: bool = False) -> list[dict]:
        """要讀回的包（day 在 [start, end] 之內，YYYY-MM-DD，含頭尾）：[{id, table, day, n}]；data=True 多一個 data（gzip 的 bytes）。"""
        self.ensure()
        where, params = ["TRUE"], []
        for cond, val in (("doc->>'table' = %s", table), ("doc->>'day' >= %s", start), ("doc->>'day' <= %s", end)):
            if val:
                where.append(cond)
                params.append(val)
        rows = self.db.query(f"SELECT id, doc{', data' if data else ''} FROM {self.table} WHERE {' AND '.join(where)} ORDER BY doc->>'day', id",
                             tuple(params))
        return [{"id": r["id"], **r["doc"], **({"data": bytes(r["data"])} if data else {})} for r in rows]

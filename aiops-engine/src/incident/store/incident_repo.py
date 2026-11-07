"""事故紀錄（PostgreSQL 的 aiops_incidents）：自動開的（status auto）由 alert 每輪更新；老師傅確認過的一個欄位都不動。"""
from __future__ import annotations

from datetime import datetime

from common.infra.pg import PgRepository, dumps, has
from common.types.incident import DEFAULT_WEIGHTS, INCIDENT_TABLE, Fingerprint


class IncidentRepository(PgRepository):
    TABLE, TIME = INCIDENT_TABLE, "start"
    INDEXES = ("((doc->>'status'))", "((doc->>'alert_id'))")

    def get_many(self, ids: list[str]) -> list[dict]:
        """這幾件（帶 id），找不到的略過。"""
        got = self._mget(ids)
        return [{"id": i, **got[i]} for i in ids if i in got]

    def find(self, statuses: tuple[str, ...], since: datetime | None = None, having: tuple[str, ...] = (),
             exclude_id: str | None = None, size: int = 1000) -> list[tuple[str, dict]]:
        """status 在 statuses、having 的欄位都有值、start ≥ since 的事故：[(id, 文件)]，新的在前。"""
        where, params = ["doc->>'status' = ANY(%s)"], [list(statuses)]
        where += [has(x) for x in having]
        if since is not None:
            where.append("ts >= %s")
            params.append(since)
        if exclude_id:
            where.append("id <> %s")
            params.append(exclude_id)
        return self._select(" AND ".join(where), params, order="ts DESC NULLS LAST, id", limit=size)

    def similar(self, fp, statuses: tuple[str, ...], exclude_id: str | None = None, size: int = 200,
                weights: dict | None = None) -> list[tuple[str, dict]]:
        """跟指紋可能像的候選：狀態對、指紋六組至少一組有交集；照「有交集的那幾組的權重加起來」排，前 size 件
        （強特徵——同一段變慢、同一個錯誤源頭——相同的一定排前面，不會被截掉）。精確的分數由 llm/similar.rank 算。"""
        fp = Fingerprint.of(fp)
        hits, params = [], []
        for field, w in (weights or DEFAULT_WEIGHTS).items():
            vals = fp.get(field)
            if vals:
                hits.append((f"(doc->'fingerprint'->'{field}') ?| %s", float(w), vals))
        if not hits:
            return []
        where = "doc->>'status' = ANY(%s) AND (" + " OR ".join(h[0] for h in hits) + ")"
        params = [list(statuses), *[h[2] for h in hits]]
        if exclude_id:
            where += " AND id <> %s"
            params.append(exclude_id)
        score = " + ".join(f"(CASE WHEN {h[0]} THEN {h[1]} ELSE 0 END)" for h in hits)
        params += [h[2] for h in hits]
        return self._select(where, params, order=f"{score} DESC, ts DESC NULLS LAST, id", limit=size)

    def recent(self, size: int = 200) -> list[tuple[str, dict]]:
        return self._select(order="ts DESC NULLS LAST, id", limit=size)

    def started_between(self, start: datetime, end: datetime) -> list[dict]:
        return [{**d, "_id": i} for i, d in self._select("ts >= %s AND ts < %s", (start, end), order="ts, id")]

    def put(self, _id: str, doc: dict) -> None:
        self._put(_id, doc)

    def patch(self, _id: str, doc: dict, upsert: dict | None = None) -> None:
        """改幾個欄位；upsert：還沒有這件時整筆建（回饋網頁比 alert 先到）。"""
        self._patch(_id, doc, upsert)

    def upsert_auto(self, incidents: list[tuple[str, dict]]) -> int:
        """只更新還是 auto 的；已被老師傅確認的（confirmed、corrected、false_positive…）一個欄位都不動。
        一個 INSERT … ON CONFLICT DO UPDATE … WHERE 做完判斷和寫入（原子），不是先讀再寫：讀和寫之間老師傅送出回饋也不會被蓋掉。
        回建立＋更新的筆數。"""
        if not incidents:
            return 0
        self.ensure()
        t, n = self.table, 0
        sql = (f"INSERT INTO {t} (id, ts, doc) VALUES (%s, %s, %s::jsonb) ON CONFLICT (id) DO UPDATE "
               f"SET doc = {t}.doc || EXCLUDED.doc, ts = COALESCE(EXCLUDED.ts, {t}.ts), updated_at = now() "
               f"WHERE COALESCE({t}.doc->>'status', 'auto') = 'auto'")
        for _id, doc in incidents:
            n += self.db.execute(sql, (_id, self._ts(doc), dumps(doc)))
        return n

"""事故紀錄（aiops-incidents）：自動開的（status auto）由 alert 每輪更新；老師傅確認過的一個欄位都不動。"""
from __future__ import annotations

from datetime import datetime

from core.entity.incident import INCIDENT_INDEX, INCIDENT_MAPPING, Fingerprint
from core.repository._base import Repository

_GUARD = ("if (ctx._source.status == null || ctx._source.status == 'auto') { ctx._source.putAll(params.doc) } else { ctx.op = 'none' }")


class IncidentRepository(Repository):
    INDEX, MAPPING = INCIDENT_INDEX, INCIDENT_MAPPING

    def get_many(self, ids: list[str]) -> list[dict]:
        """這幾件（帶 id），找不到的略過。"""
        got = self._mget(ids)
        return [{"id": i, **got[i]} for i in ids if i in got]

    def find(self, statuses: tuple[str, ...], since: datetime | None = None, having: tuple[str, ...] = (),
             exclude_id: str | None = None, size: int = 1000) -> list[tuple[str, dict]]:
        """status 在 statuses、having 的欄位都有值、start ≥ since 的事故：[(id, 文件)]。"""
        f = [{"terms": {"status": list(statuses)}}, *({"exists": {"field": x}} for x in having)]
        if since is not None:
            f.append({"range": {"start": {"gte": since.isoformat()}}})
        q = {"bool": {"filter": f}}
        if exclude_id:
            q["bool"]["must_not"] = [{"ids": {"values": [exclude_id]}}]
        return [(h["_id"], h["_source"]) for h in self._hits({"size": size, "query": q})]

    def similar(self, fp, statuses: tuple[str, ...], exclude_id: str | None = None, size: int = 200) -> list[tuple[str, dict]]:
        """跟指紋可能像的候選（Fingerprint.es_query 先篩前 size 件；分數由 service/rca/similar.rank 算）。"""
        return [(h["_id"], h["_source"]) for h in self._hits(Fingerprint.of(fp).es_query(exclude_id, size, statuses))]

    def recent(self, size: int = 200) -> list[tuple[str, dict]]:
        return [(h["_id"], h["_source"]) for h in self._hits({"size": size, "sort": [{"start": "desc"}]})]

    def started_between(self, start: datetime, end: datetime) -> list[dict]:
        body = {"query": {"bool": {"filter": [{"range": {"start": {"gte": start.isoformat(), "lt": end.isoformat()}}}]}}}
        return [{**h["_source"], "_id": h["_id"]} for h in self._scan(body)]

    def put(self, _id: str, doc: dict) -> None:
        self._put(_id, doc)

    def patch(self, _id: str, doc: dict, upsert: dict | None = None) -> None:
        """改幾個欄位；upsert：還沒有這件時整筆建（回饋網頁比 alert 先到）。"""
        self._patch(_id, doc, upsert)

    def upsert_auto(self, incidents: list[tuple[str, dict]]) -> int:
        """只更新還是 auto 的；已被老師傅確認的（confirmed、corrected、false_positive…）一個欄位都不動。
        用 ES 的 scripted upsert 一次做完判斷和寫入（原子），不是先讀再寫：讀和寫之間老師傅送出回饋也不會被蓋掉。"""
        if not incidents:
            return 0
        self.ensure()
        lines = [{"_id": _id, "script": {"lang": "painless", "source": _GUARD, "params": {"doc": doc}}, "upsert": doc} for _id, doc in incidents]
        return self._bulk_update(lines)

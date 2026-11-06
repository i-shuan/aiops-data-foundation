"""⑤ 異常總表（aiops-anomalies）：五種偵測都寫這裡；每筆寫入補 engine_version 與歸屬（dept、group）。"""
from __future__ import annotations

from datetime import datetime

from aiops import ENGINE_VERSION
from aiops.core.entity.anomaly import ANOMALY_INDEX, ANOMALY_MAPPING, RULE_FIELDS
from aiops.core.repository._base import Repository
from aiops.core.repository.owner import load_owners, owner_of


class AnomalyRepository(Repository):
    INDEX, MAPPING = ANOMALY_INDEX, ANOMALY_MAPPING

    def ensure(self, extra_fields: dict | None = None) -> None:   # noqa: D102  規則欄位一併補上
        super().ensure()
        self.add_fields({**RULE_FIELDS, **(extra_fields or {})})

    # ── 讀 ──
    def between(self, start: datetime, end: datetime, fields: list[str] | None = None, *, service_keys: tuple[str, ...] = (), service: str | None = None,
                levels: list[str] | None = None, series_regex: str | None = None, with_median: bool = False, size: int = 5000) -> list[dict]:
        """[start, end) 的異常（帶 _id），依時間排。service：keys 裡 service_keys 任一欄等於它的；levels：level 在其中；
        series_regex：序列名符合；with_median：只要有 median 的（動態基準線判的）。"""
        f = [{"range": {"@timestamp": {"gte": start.isoformat(), "lt": end.isoformat()}}}]
        if service:
            f.append({"bool": {"should": [{"term": {f"keys.{k}": service}} for k in service_keys], "minimum_should_match": 1}})
        if levels:
            f.append({"terms": {"level": levels}})
        if series_regex:
            f.append({"regexp": {"series": series_regex}})
        if with_median:
            f.append({"exists": {"field": "median"}})
        body = {"query": {"bool": {"filter": f}}, **({"_source": fields} if fields else {})}
        return [{**h["_source"], "_id": h["_id"]} for h in self._scan(body, size=size)]

    def get(self, _id: str) -> dict | None:
        return self._get(_id)

    def get_many(self, ids: list[str], fields: list[str] | None = None) -> dict[str, dict]:
        return self._mget(list(ids), fields)

    def status_of(self, ids: list[str]) -> dict[str, dict]:
        """{_id: {status, streak}}，只回 status 是 anomaly 的（上一輪判成 anomaly 的不降級）。"""
        return {i: s for i, s in self._mget(ids, ["status", "streak"]).items() if s.get("status") == "anomaly"}

    # ── 寫 ──
    def write(self, docs: list[tuple[str, dict]]) -> None:
        """每筆帶 engine_version、dept／group（keys 裡的 namespace／服務對回去，找不到 unassigned）。空的不碰 ES。"""
        if not docs:
            return
        owners = load_owners(self.es, self.scenario.owner)
        for _, d in docs:
            d["engine_version"] = ENGINE_VERSION
            d["dept"], d["group"] = owner_of(d.get("keys"), owners, self.scenario.entity_keys)
        self.bulk(docs)

    def tag_alert(self, tags: list[tuple[str, str]]) -> int:
        """把 alert_id 寫回這件併進來的每筆異常：儀表板用 alert_id:"<這件>" 就撈得到。異常已被清掉（保留期過了）的略過。"""
        if not tags:
            return 0
        Repository.ensure(self)    # mapping 是 dynamic false：舊 index 要補 alert_id 欄位，不然查不到
        return self._bulk_update([{"_id": _id, "doc": {"alert_id": aid}} for _id, aid in tags], ignore_missing=True, what=" 的 alert_id",
                                 counted=("updated", "noop"))

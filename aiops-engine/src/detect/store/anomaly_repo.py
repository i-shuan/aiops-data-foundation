"""⑤ 異常總表（PostgreSQL 的 aiops_anomalies）：五種偵測都寫這裡；每筆寫入補 engine_version 與歸屬（dept、group）。"""
from __future__ import annotations

from datetime import datetime

from common.infra.env import ENGINE_VERSION
from common.infra.pg import PgRepository, has
from common.scenario.mapping import effective
from data.store.owner_repo import load_all_owners, owner_of
from detect.store.models import ANOMALY_TABLE


class AnomalyRepository(PgRepository):
    TABLE, TIME = ANOMALY_TABLE, "@timestamp"
    INDEXES = ("((doc->>'series'), ts)", "((doc->>'level'), ts)", "((doc->>'alert_id'))")

    # ── 讀 ──
    def between(self, start: datetime, end: datetime, fields: list[str] | None = None, *, service_keys: tuple[str, ...] = (), service: str | None = None,
                levels: list[str] | None = None, series_regex: str | None = None, with_median: bool = False, size: int = 5000) -> list[dict]:
        """[start, end) 的異常（帶 _id），依時間排。service：keys 裡 service_keys 任一欄等於它的；levels：level 在其中；
        series_regex：序列名整個符合（跟 ES 的 regexp 一樣是整串比對）；with_median：只要有 median 的（動態基準線判的）。
        size 是 ES 版翻頁的大小，這裡全部讀回來、不用。"""
        where, params = ["ts >= %s", "ts < %s"], [start, end]
        if service:
            if not service_keys:
                return []
            where.append("(" + " OR ".join("doc->'keys'->>%s = %s" for _ in service_keys) + ")")
            for k in service_keys:
                params += [k, service]
        if levels:
            where.append("doc->>'level' = ANY(%s)")
            params.append(list(levels))
        if series_regex:
            where.append("doc->>'series' ~ %s")
            params.append(f"^(?:{series_regex})$")
        if with_median:
            where.append(has("median"))
        rows = self._select(" AND ".join(where), params, order="ts, id")
        return [{**({k: d[k] for k in fields if k in d} if fields else d), "_id": i} for i, d in rows]

    def get(self, _id: str) -> dict | None:
        return self._get(_id)

    def get_many(self, ids: list[str], fields: list[str] | None = None) -> dict[str, dict]:
        return self._mget(list(ids), fields)

    def status_of(self, ids: list[str]) -> dict[str, dict]:
        """{_id: {status, streak}}，只回 status 是 anomaly 的（上一輪判成 anomaly 的不降級）。"""
        return {i: s for i, s in self._mget(ids, ["status", "streak"]).items() if s.get("status") == "anomaly"}

    # ── 寫 ──
    def write(self, docs: list[tuple[str, dict]]) -> None:
        """每筆帶 engine_version、dept／group（keys 裡的 namespace／服務對回去，找不到 unassigned）。空的不碰資料庫。"""
        if not docs:
            return
        owners = load_all_owners(self.es, effective(self.scenario, self.db).owners)   # 核准過的欄位對照覆寫蓋在 yaml 上
        for _, d in docs:
            d["engine_version"] = ENGINE_VERSION
            d["dept"], d["group"] = owner_of(d.get("keys"), owners, self.scenario.entity_keys)
        self.bulk(docs)

    def tag_alert(self, tags: list[tuple[str, str]]) -> int:
        """把 alert_id 寫回這件併進來的每筆異常：儀表板用 alert_id 就撈得到。異常已被清掉（保留期過了）的略過。回對到幾筆。"""
        if not tags:
            return 0
        self.ensure()
        ids, aids = [t[0] for t in tags], [t[1] for t in tags]
        return self.db.execute(f"UPDATE {self.table} AS t SET doc = t.doc || jsonb_build_object('alert_id', v.aid), updated_at = now() "
                               f"FROM unnest(%s::text[], %s::text[]) AS v(id, aid) WHERE t.id = v.id", (ids, aids))

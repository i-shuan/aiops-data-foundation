"""調整（PostgreSQL 的 aiops_tuning）：門檻覆寫、排除時段、重置點、週期放寬、升級成規則（learn/store/models.py）。
status proposed 的是建議，adjust approve 才生效。"""
from __future__ import annotations

from common.infra.pg import PgRepository
from learn.store.models import TUNING_TABLE, validated


class TuningRepository(PgRepository):
    TABLE, TIME = TUNING_TABLE, "at"
    INDEXES = ("((doc->>'status'))",)

    def all(self) -> list[dict]:
        """全部文件（帶 id），依 at 排。讀不到（沒權限、DB 連不上）不整個失敗：印一行、回 []，這一輪只用設定檔的。"""
        try:
            rows = self._select(order="ts NULLS FIRST, id")
        except Exception as e:  # noqa: BLE001  psycopg 的連線、權限錯誤，或沒設 AIOPS_PG_DSN
            print(f"  ⚠ 讀不到 {self.TABLE}（{str(e)[:120]}），這一輪不套用裡面的調整，只用設定檔的")
            rows = []
        return [{"id": i, **d} for i, d in rows]

    def proposed(self, size: int = 50) -> list[dict]:
        return [{**d, "id": i} for i, d in self._select("doc->>'status' = 'proposed'", order="ts DESC NULLS LAST, id", limit=size)]

    def get(self, _id: str) -> dict | None:
        return self._get(_id)

    def exists(self, _id: str) -> bool:
        return self._get(_id) is not None

    def put(self, _id: str, doc: dict) -> None:
        """寫一筆（先過 Adjustment 模型：欄位名、kind、status 錯就擋）。"""
        self._put(_id, validated(doc))

    def patch(self, _id: str, doc: dict) -> None:
        self._patch(_id, doc)

    def delete(self, _id: str) -> None:
        self._delete(_id)

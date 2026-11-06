"""調整（aiops-tuning）：門檻覆寫、排除時段、重置點、週期放寬、升級成規則（learn/store/adjustment.py）。
status proposed 的是建議，adjust approve 才生效。"""
from __future__ import annotations

from learn.store.adjustment import TUNING_INDEX, TUNING_MAPPING, validated
from shared.repository import Repository


class TuningRepository(Repository):
    INDEX, MAPPING = TUNING_INDEX, TUNING_MAPPING

    def all(self) -> list[dict]:
        """全部文件（帶 id），依 at 排。讀不到（index 關閉、沒權限、ES 逾時）不整個失敗：印一行、回 []，這一輪只用設定檔的。
        index 不存在是正常的（還沒人調過）。"""
        try:
            hits = self.es.req("POST", f"/{self.INDEX}/_search", {"size": 10000, "sort": [{"at": {"order": "asc", "unmapped_type": "date"}}]},
                               ok=(200, 404)).get("hits", {}).get("hits", [])
        except Exception as e:  # noqa: BLE001  RuntimeError（ES 回錯誤）或 requests 的連線、逾時錯誤
            print(f"  ⚠ 讀不到 {self.INDEX}（{str(e)[:120]}），這一輪不套用裡面的調整，只用設定檔的")
            hits = []
        return [{"id": h["_id"], **h["_source"]} for h in hits]

    def proposed(self, size: int = 50) -> list[dict]:
        hits = self.es.req("POST", f"/{self.INDEX}/_search", {"size": size, "query": {"bool": {"filter": [{"term": {"status": "proposed"}}]}}},
                           ok=(200, 404)).get("hits", {}).get("hits", [])
        return [{**h["_source"], "id": h["_id"]} for h in hits]

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

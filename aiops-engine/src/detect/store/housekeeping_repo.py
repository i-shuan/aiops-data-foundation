"""索引層級的雜務：依時間清舊文件（prune）、放 index template（工廠的履歷索引）。索引名由設定給。"""
from __future__ import annotations

from common.infra.es import Repository

_OPTS = "ignore_unavailable=true&allow_no_indices=true"


class HousekeepingRepository(Repository):
    def count(self, index: str, query: dict) -> int:
        return int(self.es.req("POST", f"/{index}/_count?{_OPTS}", {"query": query}).get("count", 0))

    def delete_by_query(self, index: str, query: dict) -> dict:
        """刪符合的文件，refresh：刪完馬上查得到，下一輪不會重算。回 ES 的結果（deleted、failures）。"""
        return self.es.req("POST", f"/{index}/_delete_by_query?conflicts=proceed&{_OPTS}&slices=auto&wait_for_completion=true&refresh=true",
                           {"query": query})

    def put_index_template(self, name: str, body: dict) -> None:
        self.es.req("PUT", f"/_index_template/{name}", body)

"""ES 上的雜務：放 index template（工廠的履歷索引 lot-paths-*，跟 traces-merged-* 一樣留在 ES）。
engine 自己的資料清舊的在各 repository 的 prune（PostgreSQL）。"""
from __future__ import annotations

from common.infra.es import Repository


class HousekeepingRepository(Repository):
    def put_index_template(self, name: str, body: dict) -> None:
        self.es.req("PUT", f"/_index_template/{name}", body)

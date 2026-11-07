"""動態基準線（PostgreSQL 的 aiops_baseline）。"""
from __future__ import annotations

import pandas as pd

from common.clock import stored_time
from common.infra.pg import PgRepository
from detect.store.models import BASELINE_COLUMNS, BASELINE_TABLE, GROUP_COLUMNS, RENAMED

_GROUP = "COALESCE(doc->>'baseline_source', '') LIKE 'group:%%'"


class BaselineRepository(PgRepository):
    TABLE, TIME = BASELINE_TABLE, "trained_at"
    INDEXES = ("((doc->>'series'))",)

    def load(self, series: str, by: list[str]) -> pd.DataFrame:
        """這條序列自己學出來的 baseline（不含借給別人用的組 baseline）：欄位 by + BASELINE_COLUMNS。"""
        rows = [{**{f: (d.get("keys") or {}).get(f) for f in by}, **{k: d.get(RENAMED.get(k, k)) for k in BASELINE_COLUMNS}}
                for _, d in self._select(f"doc->>'series' = %s AND NOT {_GROUP}", (series,), order="id")]
        return pd.DataFrame(rows, columns=by + list(BASELINE_COLUMNS))

    def load_group(self, series: str) -> pd.DataFrame:
        """train 時從成員算出來的組 baseline：group + dow、hour、level、median、mad、resolution。"""
        rows = [{"group": d["baseline_source"][len("group:"):], **{k: d.get(k) for k in GROUP_COLUMNS}}
                for _, d in self._select(f"doc->>'series' = %s AND {_GROUP}", (series,), order="id")]
        return pd.DataFrame(rows, columns=["group", *GROUP_COLUMNS])

    def save(self, series: str, docs: list[tuple[str, dict]], trained_at: str, had_data: bool) -> None:
        """寫這次學的，再刪這條序列上一次留下、這次沒有的。查不到任何資料（例如停機）就不刪，保留舊的。"""
        self.bulk(docs)
        if docs or had_data:
            self.delete_where("doc->>'series' = %s AND ts < %s", (series, stored_time(trained_at)))

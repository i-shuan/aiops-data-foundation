"""動態基準線（aiops-baseline）。"""
from __future__ import annotations

import pandas as pd

from detect.store.models import BASELINE_COLUMNS, BASELINE_INDEX, BASELINE_MAPPING, GROUP_COLUMNS, RENAMED
from common.infra.es import Repository


class BaselineRepository(Repository):
    INDEX, MAPPING = BASELINE_INDEX, BASELINE_MAPPING

    def load(self, series: str, by: list[str]) -> pd.DataFrame:
        """這條序列自己學出來的 baseline（不含借給別人用的組 baseline）：欄位 by + BASELINE_COLUMNS。"""
        body = {"query": {"bool": {"filter": [{"term": {"series": series}}], "must_not": [{"prefix": {"baseline_source": "group:"}}]}}}
        rows = [{**{f: h["_source"]["keys"].get(f) for f in by}, **{k: h["_source"].get(RENAMED.get(k, k)) for k in BASELINE_COLUMNS}}
                for h in self._scan(body, size=10000, sort=[{"_doc": "asc"}])]
        return pd.DataFrame(rows, columns=by + list(BASELINE_COLUMNS))

    def load_group(self, series: str) -> pd.DataFrame:
        """train 時從成員算出來的組 baseline：group + dow、hour、level、median、mad、resolution。"""
        body = {"size": 10000, "query": {"bool": {"filter": [{"term": {"series": series}}, {"prefix": {"baseline_source": "group:"}}]}}}
        rows = [{"group": h["_source"]["baseline_source"][len("group:"):], **{k: h["_source"].get(k) for k in GROUP_COLUMNS}} for h in self._hits(body)]
        return pd.DataFrame(rows, columns=["group", *GROUP_COLUMNS])

    def save(self, series: str, docs: list[tuple[str, dict]], trained_at: str, had_data: bool) -> None:
        """寫這次學的，再刪這條序列上一次留下、這次沒有的。ES 查不到任何資料（例如停機）就不刪，保留舊的。"""
        self.bulk(docs)
        if docs or had_data:
            self.es.req("POST", f"/{self.INDEX}/_delete_by_query?conflicts=proceed", {"query": {"bool": {"filter": [
                {"term": {"series": series}}, {"range": {"trained_at": {"lt": trained_at}}}]}}})

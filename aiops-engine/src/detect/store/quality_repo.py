"""資料品質檢查讀的統計（detect/series_ops/quality_check.py 判斷）：每組（check 的 by）的不同值個數、缺欄位筆數、筆數、時間差百分位。索引由 check 給。"""
from __future__ import annotations

import pandas as pd

from common.infra.es import time_range
from common.infra.es import Repository


def check_keys(check: dict) -> list[str]:
    return check.get("keys") or [f.split(".")[-1] for f in check.get("by", [])]


def _query(check: dict, start, end) -> dict:
    # time_field：用哪個時間篩「最近這段」。預設 @timestamp；時鐘偏差、收資料延遲的檢查不能用 @timestamp，
    # 因為那種資料的 @timestamp 本身就錯了（8 小時前、在未來）或很舊（卡住的），會被篩掉
    f = [time_range(start, end, check.get("time_field", "@timestamp"))]
    if check.get("filter"):
        f.append(check["filter"])
    return {"bool": {"filter": f}}


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    """dtype=object：缺欄位的那組（missing_bucket）的 key 是 None，pandas 3 的字串欄位會把 None 變成 NaN，
    寫進 aiops-anomalies 的 keys 變成 JSON 的 NaN，ES 整批拒收、quality 指令中斷，訊息裡也印成 nan。"""
    return pd.DataFrame(rows, columns=columns, dtype=object)


def _missing_filter(field: str, equals: str | None) -> dict:
    """「缺」的條件：欄位不存在；有設 missing_if_equals 時，等於另一個欄位也算缺。
    Collector 沒有 service label 的 Pod，service.name 會補成 container 名稱（例如 app），欄位有值但對不到服務。
    兩個欄位都是 keyword（log 的 resource.attributes.* 是 alias，指到同一個 keyword 欄位）。"""
    absent = {"bool": {"must_not": {"exists": {"field": field}}}}
    if not equals:
        return absent
    same = {"script": {"script": {
        "source": "doc.containsKey(params.a) && doc.containsKey(params.b) && doc[params.a].size() > 0 && "
                  "doc[params.b].size() > 0 && doc[params.a].value == doc[params.b].value",
        "params": {"a": field, "b": equals}}}}
    return {"bool": {"should": [absent, same], "minimum_should_match": 1}}


class QualityRepository(Repository):
    def _groups(self, check: dict, query: dict, aggs: dict, size: int = 1000) -> list[dict]:
        """依 check 的 by 分組（composite，翻頁到底），每組一列：keys + docs + 每個子聚合。"""
        keys = check_keys(check)
        sources = [{k: {"terms": {"field": f, "missing_bucket": True}}} for k, f in zip(keys, check.get("by", []))]
        body = {"size": 0, "query": query, "aggs": {"g": {"composite": {"size": size, "sources": sources}, "aggs": aggs}}}
        return [{**b["key"], "docs": b["doc_count"], **{a: b[a] for a in aggs}} for b in self._composite(body, ("g",), check["index"])]

    def cardinality(self, check: dict, start, end, examples: int = 500) -> pd.DataFrame:
        """每組 check.field 有幾個不同的值。examples：每組抽幾個值（看像不像帶了 ID）；ref 那段只要個數，給 0。
        terms 依筆數排前面的，所以要抽到夠多：像 ID 的值每個只有幾筆，抽太少只抽到量大的樣板路由，比例被低估
        （40 條樣板＋45 條像 ID、只抽 50 個時只看到 20%）。不同值 ≤ examples 時等於全看；更多的話 max_values 會先報。"""
        keys = check_keys(check)
        aggs = {"n": {"cardinality": {"field": check["field"], "precision_threshold": 3000}}}
        if examples:
            aggs["ex"] = {"terms": {"field": check["field"], "size": examples}}
        # 一頁的組數 × 每組的值不能超過 ES 的 search.max_buckets（預設 65,536）
        size = max(10, min(1000, 60000 // (examples + 2)))
        rows = self._groups(check, _query(check, start, end), aggs, size)
        return _frame([{**{k: r[k] for k in keys}, "values": r["n"]["value"],
                        "examples": [b["key"] for b in r["ex"]["buckets"]] if examples else []} for r in rows], keys + ["values", "examples"])

    def missing(self, check: dict, start, end) -> pd.DataFrame:
        """每組幾筆、每個欄位缺幾筆（欄位 missing:<欄位>）。"""
        keys = check_keys(check)
        equals = check.get("missing_if_equals") or {}
        aggs = {f"missing:{f}": {"filter": _missing_filter(f, equals.get(f))} for f in check["fields"]}
        rows = self._groups(check, _query(check, start, end), aggs)
        return _frame([{**{k: r[k] for k in keys}, "docs": r["docs"], **{a: r[a]["doc_count"] for a in aggs}} for r in rows],
                      keys + ["docs"] + list(aggs))

    def counts(self, check: dict, start, end) -> pd.DataFrame:
        keys = check_keys(check)
        rows = self._groups(check, _query(check, start, end), {})
        return _frame([{**{k: r[k] for k in keys}, "docs": r["docs"]} for r in rows], keys + ["docs"])

    def count(self, index: str, check: dict, start, end) -> int:
        body = {"size": 0, "track_total_hits": True, "query": _query(check, start, end)}
        return int(self._search(body, index)["hits"]["total"]["value"])

    def lag(self, check: dict, start, end) -> pd.DataFrame:
        """每組的時間差百分位（秒）：check 的 to 欄位減 from 欄位。兩個欄位都要有。"""
        keys = check_keys(check)
        q = _query(check, start, end)
        q["bool"]["filter"] += [{"exists": {"field": check["from"]}}, {"exists": {"field": check["to"]}}]
        script = {"source": "doc[params.t].value.toInstant().toEpochMilli() - doc[params.f].value.toInstant().toEpochMilli()",
                  "params": {"f": check["from"], "t": check["to"]}}
        rows = self._groups(check, q, {"lag": {"percentiles": {"script": script, "percents": [50, 95]}}})
        out = []
        for r in rows:
            v = r["lag"]["values"]
            ms = lambda k: v.get(k) if v.get(k) is not None else float("nan")
            out.append({**{k: r[k] for k in keys}, "docs": r["docs"], "p50": ms("50.0") / 1000, "p95": ms("95.0") / 1000})
        return _frame(out, keys + ["docs", "p50", "p95"])

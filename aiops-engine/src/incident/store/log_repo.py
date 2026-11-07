"""log：根因分析讀的（這些請求的 log、變更紀錄、warn／error）與慢性病清單數的（每個對象每天幾筆）。索引名由設定給。
欄位名依情境（Scenario.log：我們的 k8s-* 是 resource.attributes.service.name、trace_id（alias，_source 裡沒有，要用 fields 取）；
APM 的 logs-apm.* 是 service.name、trace.id）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from common.infra.es import time_range
from common.infra.es import Repository

# log 的 @timestamp 是應用自己的時間：時鐘偏了、或請求結束後才寫的 log，可能離 span 十幾分鐘。
# trace_id 本身就夠準，時間只用來少掃幾個 backing index，所以前後多放 30 分鐘（docs/pdca/phase1-collect/2026-10-log-trace-bind.md）
LOG_TRACE_SKEW = timedelta(minutes=30)


def _to_log(hit: dict, p: dict) -> dict:
    f = {k: (v[0] if isinstance(v, list) and v else v) for k, v in hit.get("fields", {}).items()}
    return {"ts": f.get("@timestamp"), "service": f.get(p["service"]) or "?", "level": f.get(p["level"]),
            "trace_id": f.get(p["trace_id"]), "message": next((f[m] for m in p["message"] if f.get(m)), "")}


def _fields_param(p: dict) -> list[str]:
    return ["@timestamp", p["service"], p["level"], p["trace_id"], *p["message"]]


class LogRepository(Repository):
    def _profile(self) -> dict:
        return self.scenario.log

    def _logs(self, index: str, filters: list[dict], size: int, sort: list | None = None) -> list[dict]:
        p = self._profile()
        body = {"size": size, "_source": False, "fields": _fields_param(p), "query": {"bool": {"filter": filters}}, **({"sort": sort} if sort else {})}
        return [_to_log(h, p) for h in self._hits(body, index)]

    def of_traces(self, index: str, trace_ids: list[str], start: datetime, end: datetime, size: int = 2000) -> list[dict]:
        """這些請求在各服務留下的 log（log 的 trace_id 是 alias，指到 trace.id；跟 trace 的 trace_id 同名）。"""
        if not trace_ids:
            return []
        p = self._profile()
        return self._logs(index, [time_range(start - LOG_TRACE_SKEW, end + LOG_TRACE_SKEW), {"terms": {p["trace_id"]: trace_ids[:1000]}}],
                          size, [{"@timestamp": "asc"}])

    def changes(self, index: str, services: list[str], start: datetime, end: datetime, query: dict, size: int = 200) -> list[dict]:
        """這些對象在這段時間的變更紀錄（工廠：PM、recipe 變更；query 是哪些算變更，例如 {terms: {event_type: [pm, recipe_change]}}），依時間排。"""
        p = self._profile()
        return self._logs(index, [time_range(start, end), {"terms": {p["service"]: services}}, query], size, [{"@timestamp": "asc"}])

    def problems(self, index: str, services: list[str], start: datetime, end: datetime, size: int = 5000) -> list[dict]:
        """這些服務在這段時間的 warn、error、fatal log（不限有沒有 trace_id：有些錯誤不在請求裡，例如背景工作）。
        來源有 problem_query 的（APM 的例外文件沒有 log.level，用 processor.event: error 認）一起撈。"""
        p = self._profile()
        level = {"terms": {p["level"]: p["problem_levels"]}}
        which = {"bool": {"should": [level, p["problem_query"]], "minimum_should_match": 1}} if p.get("problem_query") else level
        return self._logs(index, [time_range(start, end), {"terms": {p["service"]: services}}, which], size)

    def daily_counts(self, index: str, by: list[str], keys: list[str], start, end, tz: str, filter: dict | None = None) -> pd.DataFrame:
        """每個對象（by 的欄位，叫 keys）每天幾筆（當地時間）：欄位 keys + day + count。"""
        f = [time_range(start, end)] + ([filter] if filter else [])
        sources = [{k: {"terms": {"field": fld, "missing_bucket": True}}} for k, fld in zip(keys, by)]
        sources.append({"day": {"date_histogram": {"field": "@timestamp", "calendar_interval": "1d", "time_zone": tz}}})
        body = {"size": 0, "query": {"bool": {"filter": f}}, "aggs": {"g": {"composite": {"size": 1000, "sources": sources}}}}
        rows = [{**{x: b["key"][x] for x in keys}, "count": b["doc_count"],
                 "day": pd.Timestamp(b["key"]["day"], unit="ms", tz="UTC").tz_convert(tz).date()} for b in self._composite(body, ("g",), index)]
        return pd.DataFrame(rows, columns=keys + ["day", "count"])

"""自我檢查：宣告的來源跟 ES 裡真的資料對不對。只回報、不擋（呼叫端印出來、寫一筆狀態文件讓 Grafana 叫）。

宣告錯了（資料其實是 APM、卻設成 OTel；索引名或欄位名打錯）的症狀不是錯誤，是「查不到資料」：序列 0 筆、沒有異常，
看起來像一切正常。這裡每次對每個索引抽幾筆文件，確認：
  - raw trace 的索引：文件是不是宣告的那個適配器認得的格式（`SourceAdapter.can_handle`）
  - 序列的索引：序列要用的欄位（`by`）在抽到的文件裡有沒有出現
連不上 ES、索引不存在都只算「查不到」，不丟例外：自我檢查不能自己變成故障。
"""
from __future__ import annotations

from dataclasses import dataclass

from common.infra.es import get_field
from data.source.adapter import SourceAdapter

SAMPLE = 20           # 每個索引抽幾筆（欄位不一定每筆都有，例如 route 只有 HTTP 請求才有）


@dataclass(frozen=True)
class Finding:
    where: str        # 哪個索引（哪條序列）
    problem: str      # 哪裡不對、可能是什麼原因
    kind: str = "mismatch"      # mismatch＝抽到資料但跟宣告對不上（欄位一次都沒出現、格式不是宣告的那種）；nodata＝抽不到資料（可能只是真的沒有）；unchecked＝檢查不了（連不上）

    @property
    def counts(self) -> bool:
        """算不算「對不上」（Grafana 的 source_mismatch 只算這種）：抽不到資料可能只是這個索引本來就沒有（例如沒開 log shipping），不叫人。"""
        return self.kind == "mismatch"

    def __str__(self) -> str:
        return f"{self.where}：{self.problem}"


def _sample(es, index: str, query: dict | None) -> tuple[list[dict] | None, str | None]:
    """(抽到的 _source 清單, 錯誤)。索引不存在或沒資料是空清單；連不上回 (None, 原因)。"""
    body = {"size": SAMPLE, "query": query or {"match_all": {}}, "sort": [{"@timestamp": "desc"}]}
    try:
        res = es.req("POST", f"/{index}/_search?ignore_unavailable=true&allow_no_indices=true", body)
    except Exception as e:  # noqa: BLE001  連不上、權限不夠：回報成「檢查不了」，不丟出去
        return None, f"{type(e).__name__}: {str(e)[:120]}"
    return [h["_source"] for h in res.get("hits", {}).get("hits", [])], None


def check_trace_index(es, index: str, adapter: SourceAdapter, adapters: dict[str, SourceAdapter] | None = None) -> list[Finding]:
    """raw trace 索引：抽到的文件要是宣告的適配器認得的格式。adapters 是登記表（給了才能說「文件長得像哪個來源」）。"""
    docs, err = _sample(es, index, None)
    if docs is None:
        return [Finding(index, f"檢查不了（{err}）", "unchecked")]
    if not docs:
        return [Finding(index, f"沒有資料（宣告的來源是 {adapter.name}）：索引名不對、或這段時間沒有 trace", "nodata")]
    bad = [d for d in docs if not adapter.can_handle(d)]
    if len(bad) == len(docs):
        others = [n for n, a in (adapters or {}).items() if a is not adapter and any(a.can_handle(d) for d in docs)]
        hint = f"；文件長得像 {'、'.join(others)}" if others else f"；文件有的欄位：{sorted(docs[0])[:10]}"
        return [Finding(index, f"宣告的來源是 {adapter.name}，但抽到的 {len(docs)} 筆都不是這個格式{hint}")]
    return []


def check_fields(es, name: str, index: str, query: dict | None, fields: list[str]) -> list[Finding]:
    """序列的索引：要用的欄位在抽到的文件裡至少出現一次。"""
    docs, err = _sample(es, index, query)
    if docs is None:
        return [Finding(name, f"檢查不了（{err}）", "unchecked")]
    if not docs:
        return [Finding(name, f"{index} 沒有符合的資料：索引名、filter 不對，或這段時間真的沒資料（例如沒開 log shipping）", "nodata")]
    missing = [f for f in fields if all(get_field(d, f) is None for d in docs)]
    if missing:
        return [Finding(name, f"{index} 抽到 {len(docs)} 筆，但這些欄位一次都沒出現：{'、'.join(missing)}（欄位名跟資料不一樣？來源宣告錯了？）")]
    return []

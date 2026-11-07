"""② 清資料的「來源」：OTel、Elastic APM 各一個 SourceAdapter（adapter.py 寫明一種來源要回答什麼）。

用法：
  adapter_of("apm")                依名字取（宣告的來源：AIOPS_SOURCE、Scenario.source）
  for_index(索引名, 宣告的來源)    查 raw trace 之前要決定怎麼查：APM 認領自己的索引（traces-apm-*），其他用宣告的來源
  to_span(raw 文件)                raw 文件 → 統一的 span：問每個適配器 can_handle，第一個認得的翻；都不認得丟 UnknownSourceError
  ALIASES                          調整的 match 可以互換的欄位名（各來源的聯集）
來源判斷只在這個資料夾：別處不要寫「是不是 apm」（tests/test_source.py 用 AST 擋）。
"""
from __future__ import annotations

from data.source.apm import ApmAdapter
from data.source.adapter import SourceAdapter
from data.source.otel import OtelAdapter

DEFAULT = "otel"
ADAPTERS: dict[str, SourceAdapter] = {f.name: f for f in (ApmAdapter(), OtelAdapter())}      # 順序＝問的順序（要積極認的先問）
ALIASES: list[tuple[str, ...]] = [a for f in ADAPTERS.values() for a in f.aliases]


def adapter_of(name: str) -> SourceAdapter:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise ValueError(f"不認得的原始資料格式 {name!r}，只能是 {'、'.join(ADAPTERS)}") from None


def for_index(index: str, declared: str = DEFAULT) -> SourceAdapter:
    """這個 raw 索引用哪個來源讀：有來源認領（索引名）就用它，沒有就用宣告的。"""
    claimed = next((f for f in ADAPTERS.values() if f.owns_index(index)), None)
    # 宣告的來源不在登記表裡（工廠情境的 source 是 "factory"：資料是 MES 履歷，沒有 raw trace）就用預設；跟抽出來之前「非 apm 都當 otel」一樣
    return claimed or ADAPTERS.get(declared) or ADAPTERS[DEFAULT]


class UnknownSourceError(ValueError):
    """這筆 raw 文件沒有任何適配器認得（不是 OTel 也不是 APM 的 span）：多半是索引指錯、或來源的欄位改了。"""


def to_span(doc: dict) -> dict:
    """raw 文件 → 統一的 span。"""
    for a in ADAPTERS.values():
        if a.can_handle(doc):
            return a.to_span(doc)
    keys = sorted(doc)[:12]
    raise UnknownSourceError(f"認不得這筆 raw 文件的格式（已知：{'、'.join(ADAPTERS)}）；欄位：{keys}")

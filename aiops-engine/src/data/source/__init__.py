"""② 清資料的「來源」：OTel、Elastic APM 各一個 SourceFormat（base.py 寫明一種來源要提供什麼）。

用法：
  format_of("apm")                 依名字取（宣告的來源：AIOPS_SOURCE、Scenario.source）
  for_index(索引名, 宣告的來源)    查 raw trace 之前要決定怎麼查：APM 認領自己的索引（traces-apm-*），其他用宣告的來源
  to_span(raw 文件)                raw 文件 → 統一的 span：看內容（APM 有 processor.event／trace.id），不是 APM 的就當 OTel
  ALIASES                          調整的 match 可以互換的欄位名（各來源的聯集）
來源判斷只在這個資料夾：別處不要寫「是不是 apm」（tests/test_source.py 用 AST 擋）。
"""
from __future__ import annotations

from data.source.apm import ApmFormat
from data.source.base import SourceFormat
from data.source.otel import OtelFormat

DEFAULT = "otel"
FORMATS: dict[str, SourceFormat] = {f.name: f for f in (ApmFormat(), OtelFormat())}      # 順序＝認領的順序；OTel 是沒人認領時的預設
ALIASES: list[tuple[str, ...]] = [a for f in FORMATS.values() for a in f.aliases]


def format_of(name: str) -> SourceFormat:
    try:
        return FORMATS[name]
    except KeyError:
        raise ValueError(f"不認得的原始資料格式 {name!r}，只能是 {'、'.join(FORMATS)}") from None


def for_index(index: str, declared: str = DEFAULT) -> SourceFormat:
    """這個 raw 索引用哪個來源讀：有來源認領（索引名）就用它，沒有就用宣告的。"""
    claimed = next((f for f in FORMATS.values() if f.owns_index(index)), None)
    # 宣告的來源不在登記表裡（工廠情境的 source 是 "factory"：資料是 MES 履歷，沒有 raw trace）就用預設；跟抽出來之前「非 apm 都當 otel」一樣
    return claimed or FORMATS.get(declared) or FORMATS[DEFAULT]


def to_span(doc: dict) -> dict:
    """raw 文件 → 統一的 span。"""
    return next((f for f in FORMATS.values() if f.name != DEFAULT and f.is_doc(doc)), FORMATS[DEFAULT]).to_span(doc)

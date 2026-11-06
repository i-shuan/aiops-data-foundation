"""資料來源 profile 的門面：engine 讀的原始資料是誰寫的，決定程式裡寫死的那幾個欄位名與索引名。內容在 plugin（core/plugin.py）：

  owner   dept／group 歸屬查哪個索引、哪幾個欄位（core/owner.py）
  log     根因分析讀 log 的欄位（investigator/es.py）

序列、規則、Pod 同儕比對的欄位全在 YAML（configs/<來源>/*.yaml），不經過這裡。
IT（plugins/observability）用 AIOPS_SOURCE 再選 otel／apm；工廠（plugins/factory）只有一種。
"""
from __future__ import annotations

from . import plugin


def name() -> str:
    """原始資料的格式名：otel、apm、factory…（core/traces.schema_of 用它判斷 raw 索引是不是 APM）。"""
    return plugin.current().source_name()


def profile(section: str) -> dict:
    return plugin.current().source_profile()[section]

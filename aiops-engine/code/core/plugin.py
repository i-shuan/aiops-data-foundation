"""主程式的擴充點（plugin）。engine 一份程式，情境（IT 可觀測性、全套工廠、只有指標）各是一個 plugin 套件，
用環境變數 AIOPS_PLUGIN 選；也可以自己寫一個掛上去（docs/pdca/meta/2026-11-plugin-architecture.md、plugins/README.md）。

  AIOPS_PLUGIN=observability    （預設）IT：服務、API、trace 的呼叫關係；AIOPS_SOURCE 再選 otel／apm 的欄位對照
  AIOPS_PLUGIN=factory          工廠：機台、recipe、履歷（lot-paths-*）的前後站、recipe 變更當換版
  AIOPS_PLUGIN=metrics_only     只有 ML／FDC 判好的指標、沒有履歷：不併跨機台、報告略過履歷那幾節
  AIOPS_PLUGIN=套件.模組:類別    客製：繼承 Plugin（或任一內建 plugin）覆寫要改的那幾個

主程式（core／detector／investigator／api）只透過 current() 問 plugin，不認得 factory、lot、equipment 這些字。
擴充點只開「現在真的有兩種以上做法」的地方：

  source_profile()   索引、欄位名的對照（owner 歸屬查哪裡、log 的欄位）        detector/owner.py、investigator/rca/queries.py
  domain()           用語、回饋表單的分類表                                       這個檔底下的門面函式、api 的回饋表單、報告
  entity_keys        異常 keys 裡哪個欄位是「對象」（服務／機台）                 investigator/alerts/merge.py、detector/owner.py
  route_of(keys)     「哪支 API」（IT 是 method＋route；工廠是 recipe）            alerts.route_of、根因分析挑 lot
  chain(...)         跨對象併件的依據（呼叫關係／履歷前後站／不併）              investigator/alerts/command.py → merge.merge
  change_at(...)     「換版」時間（版號第一次出現／變更紀錄最近一筆／沒有）      alerts/command.grade_alert → severity 的上限
  severity_classes   嚴重度的訊號分類預設（IT 黃金訊號／工廠參數家族）           cfg 的 alerts.severity.classes 可覆蓋
  critical_levels    哪些 level 的 critical 異常直接讓這件 critical
  sections           根因分析報告有哪幾段、順序（investigator/rca/report.py 的 SECTIONS）
  commands()         這個情境的 detector 子命令：{名: register(app)}，掛到 typer app（IT 的 peer／quality／chronic／trace-*、
                     工廠的 ingest／paths-merge）
  schedule           預設排程表（chart 的 deployment 依 plugin 選）

門面（原 core/domain.py、core/source.py，已併進來）：source_name()、source_profile(section)；domain(section) 與 entity_label() 等用語函式。
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

ENV = "AIOPS_PLUGIN"
DEFAULT = "observability"
BUILTIN = {
    "observability": "plugins.observability:Plugin",
    "factory": "plugins.factory:Plugin",
    "metrics_only": "plugins.metrics_only:Plugin",
}


@dataclass
class Chain:
    """跨對象併件的依據（investigator/alerts/merge.merge 的 calls、links）。
    calls：{(上游, 下游)}，下游出事連帶上游（IT 的呼叫關係）。
    links：{異常 _id: [(前一個對象, 這個單位在那裡的時間), …]}，後站的異常依單位併進前站（工廠的履歷）。兩個都空＝不併。"""
    calls: set[tuple[str, str]] = field(default_factory=set)
    links: dict[str, list[tuple[str, datetime]]] = field(default_factory=dict)


class Plugin:
    """基底：什麼都不併、沒有換版、沒有額外命令。內建的三個在 plugins/。"""
    name: str = "base"
    entity_keys: tuple[str, ...] = ("service",)
    critical_levels: tuple[str, ...] = ("rule", "peer", "quality")
    severity_classes: dict | list | None = None          # None＝investigator/severity.CLASSES（IT 黃金訊號）
    sections: tuple[str, ...] = ("anomalies", "entries", "segments", "slowest", "logs", "changes", "topology", "conclusion")
    schedule: str = "configs/schedule/schedule.yaml"

    def source_profile(self) -> dict:
        """{"owner": {...}, "log": {...}}：見 plugins/observability 的 PROFILES。"""
        raise NotImplementedError

    def source_name(self) -> str:
        """原始資料的格式名（sources/traces.schema_of 用它判斷 raw 索引是不是 APM）。"""
        return self.name

    def domain(self) -> dict:
        """entity／unit／trace／new_value_hint／categories／fp_reasons／checked：見 plugins/observability 的 DOMAIN。"""
        raise NotImplementedError

    def route_of(self, keys: dict) -> str | None:
        return None

    def chain(self, es, cfg: dict, anomalies: list[dict], open_alerts: list[dict], start: datetime, now: datetime) -> Chain:
        return Chain()

    def change_at(self, es, cfg: dict, entity: str, since: datetime, now: datetime) -> datetime | None:
        return None

    def commands(self) -> dict[str, Callable]:
        """{子命令名: register(app)}：register 把子命令掛到 detector 的 typer app（app.command(名, help=…)(函式)），
        跟內建子命令同一種形狀（detector/__main__.py 的 COMMANDS）；函式自己讀設定（core.config.load）、呼叫處理函式。"""
        return {}


_cache: tuple[str, Plugin] | None = None


def spec() -> str:
    return os.environ.get(ENV) or DEFAULT


def load(s: str | None = None) -> Plugin:
    """依名字（BUILTIN）或 "套件.模組:類別" 載入一個新的實例；不快取。"""
    s = s or spec()
    target = BUILTIN.get(s, s)
    if ":" not in target:
        raise ValueError(f"{ENV} 只能是 {'、'.join(BUILTIN)} 或「套件.模組:類別」，不是 {s!r}")
    mod, _, cls = target.partition(":")
    try:
        obj = getattr(importlib.import_module(mod), cls)
    except (ImportError, AttributeError) as e:
        raise ValueError(f"{ENV}={s!r} 載入不了：{e}") from e
    p = obj() if isinstance(obj, type) else obj
    if not isinstance(p, Plugin):
        raise ValueError(f"{ENV}={s!r} 不是 core.plugin.Plugin")
    return p


def current() -> Plugin:
    """現在生效的 plugin；環境變數變了（測試）會自動換。"""
    global _cache
    s = spec()
    if _cache is None or _cache[0] != s:
        _cache = (s, load(s))
    return _cache[1]


def reset() -> None:
    global _cache
    _cache = None


# ── 資料來源 profile 的門面：engine 讀的原始資料是誰寫的，決定程式裡寫死的那幾個欄位名與索引名 ──
#   owner   dept／group 歸屬查哪個索引、哪幾個欄位（detector/owner.py）
#   log     根因分析讀 log 的欄位（investigator/rca/queries.py）
# 序列、規則、Pod 同儕比對的欄位全在 YAML（configs/<來源>/*.yaml），不經過這裡。IT 用 AIOPS_SOURCE 再選 otel／apm；工廠只有一種。

def source_name() -> str:
    """原始資料的格式名：otel、apm、factory…（sources/traces.schema_of 用它判斷 raw 索引是不是 APM）。"""
    return current().source_name()


def source_profile(section: str) -> dict:
    return current().source_profile()[section]


# ── 領域用語的門面：對象叫什麼（服務／機台）、一個單位叫什麼（請求／lot）、回饋表單的分類表、誤報原因、常查的地方 ──
# 內容在 plugin 的 domain()；報告、LLM 範本、api 的回饋表單都從這裡拿。

def domain(section: str):
    return current().domain()[section]


def categories() -> list[tuple[str, str]]:
    return list(domain("categories"))


def category_codes() -> tuple[str, ...]:
    return tuple(c for c, _ in categories())


def fp_reasons() -> list[tuple[str, str]]:
    return list(domain("fp_reasons"))


def checked() -> list[str]:
    return list(domain("checked"))


def entity_label() -> str:
    """對象叫什麼：服務／機台。"""
    return domain("entity")


def unit_label() -> str:
    """被追蹤的一個單位叫什麼：請求／lot（報告、摘要的措辭）。"""
    return domain("unit")


def unit_text() -> str:
    """unit_label 放進中文句子用：英文的（lot）前後補空格，「每個 lot 多花」。"""
    u = unit_label()
    return f" {u} " if u.isascii() else u


def trace_label() -> str:
    """把一個單位串起來的那份資料叫什麼：Trace／履歷。"""
    return domain("trace")


def new_value_hint() -> str:
    """共同點是「平常沒見過的值」時，提示可能是什麼。"""
    return domain("new_value_hint")

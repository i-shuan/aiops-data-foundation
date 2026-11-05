"""主程式的擴充點（plugin）。engine 一份程式，情境（IT 可觀測性、全套工廠、只有指標）各是一個 plugin 套件，
用環境變數 AIOPS_PLUGIN 選；也可以自己寫一個掛上去（docs/pdca/meta/2026-11-plugin-architecture.md、plugins/README.md）。

  AIOPS_PLUGIN=observability    （預設）IT：服務、API、trace 的呼叫關係；AIOPS_SOURCE 再選 otel／apm 的欄位對照
  AIOPS_PLUGIN=factory          工廠：機台、recipe、履歷（lot-paths-*）的前後站、recipe 變更當換版
  AIOPS_PLUGIN=metrics_only     只有 ML／FDC 判好的指標、沒有履歷：不併跨機台、報告略過履歷那幾節
  AIOPS_PLUGIN=套件.模組:類別    客製：繼承 Plugin（或任一內建 plugin）覆寫要改的那幾個

主程式（core／detector／investigator／feedback_web）只透過 current() 問 plugin，不認得 factory、lot、equipment 這些字。
擴充點只開「現在真的有兩種以上做法」的地方：

  source_profile()   索引、欄位名的對照（owner 歸屬查哪裡、log 的欄位）        detector/owner.py、investigator/es.py
  domain()           用語、回饋表單的分類表                                       core/domain.py 的門面、feedback_web、報告
  entity_keys        異常 keys 裡哪個欄位是「對象」（服務／機台）                 investigator/alerts.py、detector/owner.py
  route_of(keys)     「哪支 API」（IT 是 method＋route；工廠是 recipe）            alerts.route_of、根因分析挑 lot
  chain(...)         跨對象併件的依據（呼叫關係／履歷前後站／不併）              investigator/cmd_alert.py → alerts.merge
  change_at(...)     「換版」時間（版號第一次出現／變更紀錄最近一筆／沒有）      cmd_alert.grade_alert → severity 的上限
  severity_classes   嚴重度的訊號分類預設（IT 黃金訊號／工廠參數家族）           cfg 的 alerts.severity.classes 可覆蓋
  critical_levels    哪些 level 的 critical 異常直接讓這件 critical
  sections           根因分析報告有哪幾段、順序（investigator/report.py 的 SECTIONS）
  commands()         這個情境多出來的 detector 子命令（例如工廠的 paths-merge）
  schedule           預設排程表（chart 的 deployment 依 plugin 選）
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
    """跨對象併件的依據（investigator/alerts.merge 的 calls、links）。
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
        """原始資料的格式名（core/traces.schema_of 用它判斷 raw 索引是不是 APM）。"""
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

    def commands(self) -> dict[str, tuple[Callable, Callable]]:
        """{子命令名: (加 parser 的函式 add(sub), 處理函式 handler(cfg, args))}。"""
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

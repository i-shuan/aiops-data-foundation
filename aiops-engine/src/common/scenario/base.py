"""情境（Scenario）：engine 一份程式，IT 可觀測性、全套工廠、只有指標各是一份資料檔（同資料夾的 *.yaml）加兩個策略。
主程式不認得 factory、lot、equipment 這些字，只看 Scenario 的欄位；在組合根（cli／web／jobs 的入口、Repos）載入一次、往下傳。

  環境變數 AIOPS_PLUGIN：observability（預設；再用 AIOPS_SOURCE 選 otel／apm）、factory、metrics_only，
  或「套件.模組:名字」指到一個 Scenario 實例（客製：dataclasses.replace(load_scenario("factory"), …)），或一份 yaml 的路徑。

一份情境有什麼（跟同資料夾 *.yaml 的欄位一樣）：
  source           原始資料格式（otel／apm／factory）：trace 的 schema、owner／log 的欄位對照跟著它
  owner、log       歸屬查哪個索引哪幾個欄位；log 的欄位名（detect/store/owner_repo.py、log.py）
  entity、unit、trace、new_value_hint、categories、fp_reasons、checked   用語與回饋表單的分類表（報告、LLM 範本、網頁）
  entity_keys      異常 keys 裡哪個欄位是「對象」（服務／機台）
  route            「哪支 API」怎麼算：http（trace 入口的名稱、method＋route）、recipe、none
  chain、change    兩個策略：跨對象併件的依據（呼叫關係／履歷前後站／不併）、「換版」（版號第一次出現／變更紀錄最近一筆／沒有）
  severity_classes、critical_levels、sections、schedule、commands
"""
from __future__ import annotations

from common.config.settings import settings

import importlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

import yaml

if TYPE_CHECKING:      # 只給型別檢查看；common 不依賴功能資料夾
    from incident.config.rca import RcaConfig

ENV = "AIOPS_PLUGIN"
SOURCE_ENV = "AIOPS_SOURCE"
DEFAULT = "observability"
DATA = Path(__file__).resolve().parent
BUILTIN = {"observability": {"otel": "it_otel", "apm": "it_apm"}, "factory": "factory", "metrics_only": "metrics_only"}


@dataclass
class Chain:
    """跨對象併件的依據（incident/alert_grouper 的 calls、links）。
    calls：{(上游, 下游)}，下游出事連帶上游（IT 的呼叫關係）。
    links：{異常 _id: [(前一個對象, 這個單位在那裡的時間), …]}，後站的異常依單位併進前站（工廠的履歷）。兩個都空＝不併。"""
    calls: set[tuple[str, str]] = field(default_factory=set)
    links: dict[str, list[tuple[str, datetime]]] = field(default_factory=dict)


class ChainStrategy(Protocol):
    def __call__(self, repo, cfg: dict, anomalies: list[dict], open_alerts: list, start: datetime, now: datetime) -> Chain: ...


class ChangeStrategy(Protocol):
    def __call__(self, repo, rca_config: RcaConfig, entity: str, since: datetime, now: datetime) -> datetime | None: ...


class NoChain:
    def __call__(self, repo, rca_config, anomalies, open_alerts, start, now) -> Chain:
        return Chain()


class NoChange:
    def __call__(self, repo, rca_config, entity, since, now) -> datetime | None:
        return None


def http_route(keys: dict) -> str | None:
    """IT 的「哪支 API」：trace 入口 span 的名稱、或 method＋route；APM 的 transaction.name 本來就是 "GET /api/x"，只有 route。"""
    if keys.get("entry.name"):
        return keys["entry.name"]
    for m, r in (("attributes.http.request.method", "attributes.http.route"), ("method", "route")):
        if keys.get(m) and keys.get(r):
            return f"{keys[m]} {keys[r]}"
    if keys.get("route"):
        return str(keys["route"])
    return None


def recipe_route(keys: dict) -> str | None:
    """工廠的「哪支 API」是哪個 recipe（履歷裡站的 name 就是 recipe，根因分析用它挑 lot）。"""
    return str(keys["recipe"]) if keys.get("recipe") else None


ROUTES: dict[str, Callable[[dict], str | None]] = {"http": http_route, "recipe": recipe_route, "none": lambda keys: None}


@dataclass(frozen=True)
class Scenario:
    name: str
    source: str
    owner: dict
    log: dict
    entity: str
    unit: str
    trace: str
    new_value_hint: str
    categories: tuple[tuple[str, str], ...]
    fp_reasons: tuple[tuple[str, str], ...]
    checked: tuple[str, ...]
    entity_keys: tuple[str, ...]
    route: str = "http"
    chain: ChainStrategy = field(default_factory=NoChain)
    change: ChangeStrategy = field(default_factory=NoChange)
    critical_levels: tuple[str, ...] = ("rule", "peer", "quality")
    severity_classes: dict | None = None          # None＝incident/alert_severity.CLASSES（IT 黃金訊號）
    sections: tuple[str, ...] = ("anomalies", "entries", "segments", "slowest", "logs", "changes", "topology", "conclusion")
    schedule: str = "configs/schedule/schedule.yaml"
    commands: dict[str, Callable] = field(default_factory=dict)   # 多出來的 detector 子命令：{名: register(app)}

    def route_of(self, keys: dict | None) -> str | None:
        return ROUTES[self.route](keys or {})

    def category_codes(self) -> tuple[str, ...]:
        return tuple(c for c, _ in self.categories)

    def unit_text(self) -> str:
        """unit 放進中文句子用：英文的（lot）前後補空格，「每個 lot 多花」。"""
        return f" {self.unit} " if self.unit.isascii() else self.unit


def _import(path: str):
    mod, _, name = path.partition(":")
    try:
        return getattr(importlib.import_module(mod), name)
    except (ImportError, AttributeError) as e:
        raise ValueError(f"載入不了 {path!r}：{e}") from e


def _read(name_or_path: str) -> dict:
    p = Path(name_or_path) if name_or_path.endswith((".yaml", ".yml")) else DATA / f"{name_or_path}.yaml"
    with open(p, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    if d.get("extends"):
        base = _read(d.pop("extends"))
        d = {**base, **d}
    return d


def from_dict(d: dict) -> Scenario:
    dom = d.get("domain") or {}
    strategy = lambda key, none: _import(d[key]["strategy"])(**(d[key].get("params") or {})) if (d.get(key) or {}).get("strategy") else none()
    return Scenario(
        name=d["name"], source=d.get("source", "otel"), owner=d.get("owner") or {}, log=d.get("log") or {},
        entity=dom.get("entity", "服務"), unit=dom.get("unit", "請求"), trace=dom.get("trace", "Trace"), new_value_hint=dom.get("new_value_hint", ""),
        categories=tuple(tuple(c) for c in dom.get("categories") or ()), fp_reasons=tuple(tuple(c) for c in dom.get("fp_reasons") or ()),
        checked=tuple(dom.get("checked") or ()),
        entity_keys=tuple(d.get("entity_keys") or ("service",)), route=d.get("route", "http"),
        chain=strategy("chain", NoChain), change=strategy("change", NoChange),
        critical_levels=tuple(d.get("critical_levels") or ("rule", "peer", "quality")), severity_classes=d.get("severity_classes"),
        sections=tuple(d.get("sections") or Scenario.sections), schedule=d.get("schedule", Scenario.schedule),
        commands={k: _import(v) for k, v in (d.get("commands") or {}).items()})


def load_scenario(spec: str | None = None) -> Scenario:
    """環境變數（或 spec）→ Scenario。內建三個名字、yaml 路徑、或「套件.模組:名字」（Scenario 實例，或回 Scenario 的函式）。"""
    cfg = settings()
    s = spec or cfg.aiops_plugin or DEFAULT
    if s in BUILTIN:
        target = BUILTIN[s]
        if isinstance(target, dict):
            src = cfg.aiops_source
            if src not in target:
                raise ValueError(f"{SOURCE_ENV} 只能是 {'、'.join(target)}，不是 {src!r}")
            target = target[src]
        return from_dict(_read(target))
    if s.endswith((".yaml", ".yml")):
        return from_dict(_read(s))
    if ":" not in s:
        raise ValueError(f"{ENV} 只能是 {'、'.join(BUILTIN)}、一份 yaml 的路徑，或「套件.模組:名字」，不是 {s!r}")
    obj = _import(s)
    sc = obj() if callable(obj) and not isinstance(obj, Scenario) else obj
    if not isinstance(sc, Scenario):
        raise ValueError(f"{ENV}={s!r} 不是 Scenario")
    return sc

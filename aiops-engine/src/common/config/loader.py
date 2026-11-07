"""讀檔、驗證、錯誤訊息；KINDS 與 MODELS（設定種類 → 模型）。"""
from __future__ import annotations

import importlib
import types
import typing
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, overload

import yaml
from pydantic import BaseModel, ValidationError

from common.config.common import Strict

if TYPE_CHECKING:        # 只給型別檢查看（overload 的回傳型別）；執行時不 import，common 不依賴任何功能資料夾
    from detect.config.adjustments import AdjustmentsConfig
    from detect.config.calendar import CalendarConfig
    from detect.config.chronic import ChronicConfig
    from detect.config.ingest import IngestConfig
    from detect.config.paths import PathsConfig
    from detect.config.peer import PeerConfig
    from detect.config.prune import PruneConfig
    from detect.config.quality import QualityConfig
    from detect.config.rules import RulesConfig
    from detect.config.series import SeriesConfig
    from detect.config.trace import TraceConfig
    from incident.config.rca import RcaConfig
    from interface.config.schedule import ScheduleConfig


# kind → "模組:類別"。模型各自放在用它的功能資料夾（config/），第一次用到才載入（跟 Repos 的 REGISTRY 同一招）
REGISTRY: dict[str, str] = {
    "series": "detect.config.series:SeriesConfig", "rules": "detect.config.rules:RulesConfig", "peer": "detect.config.peer:PeerConfig",
    "quality": "detect.config.quality:QualityConfig", "trace": "detect.config.trace:TraceConfig", "chronic": "detect.config.chronic:ChronicConfig",
    "prune": "detect.config.prune:PruneConfig", "calendar": "detect.config.calendar:CalendarConfig",
    "adjustments": "detect.config.adjustments:AdjustmentsConfig", "ingest": "detect.config.ingest:IngestConfig",
    "paths": "detect.config.paths:PathsConfig", "rca": "incident.config.rca:RcaConfig", "schedule": "interface.config.schedule:ScheduleConfig",
}
KINDS = tuple(REGISTRY)


def model_of(kind: str) -> type[Strict]:
    """設定種類 → 模型類別（第一次用到才 import 那個功能資料夾的 config）。"""
    module, _, name = REGISTRY[kind].partition(":")
    return getattr(importlib.import_module(module), name)


class _Models(Mapping):
    """MODELS["series"] → SeriesConfig（懶載入）。"""
    def __getitem__(self, kind): return model_of(kind)
    def __iter__(self): return iter(REGISTRY)
    def __len__(self): return len(REGISTRY)


MODELS: Mapping[str, type[Strict]] = _Models()


class ConfigError(ValueError):
    """設定檔不對。str() 是一行一個問題：<檔名>: <欄位路徑>：<問題>。"""


@overload
def load_config(path: str, kind: Literal["series"]) -> SeriesConfig: ...
@overload
def load_config(path: str, kind: Literal["rules"]) -> RulesConfig: ...
@overload
def load_config(path: str, kind: Literal["peer"]) -> PeerConfig: ...
@overload
def load_config(path: str, kind: Literal["quality"]) -> QualityConfig: ...
@overload
def load_config(path: str, kind: Literal["trace"]) -> TraceConfig: ...
@overload
def load_config(path: str, kind: Literal["rca"]) -> RcaConfig: ...
@overload
def load_config(path: str, kind: Literal["schedule"]) -> ScheduleConfig: ...
@overload
def load_config(path: str, kind: Literal["chronic"]) -> ChronicConfig: ...
@overload
def load_config(path: str, kind: Literal["prune"]) -> PruneConfig: ...
@overload
def load_config(path: str, kind: Literal["calendar"]) -> CalendarConfig: ...
@overload
def load_config(path: str, kind: Literal["adjustments"]) -> AdjustmentsConfig: ...
@overload
def load_config(path: str, kind: Literal["ingest"]) -> IngestConfig: ...
@overload
def load_config(path: str, kind: Literal["paths"]) -> PathsConfig: ...
@overload
def load_config(path: str, kind: str) -> Strict: ...
def load_config(path: str, kind: str) -> Strict:
    """讀一份設定檔。kind 是哪一種（KINDS），用它挑模型驗證。回傳那一種的模型（SeriesConfig、RulesConfig…），不是 dict。"""
    if kind not in KINDS:
        raise ValueError(f"不認得的設定種類 {kind!r}，只能是 {'、'.join(KINDS)}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 最上層要是 mapping（鍵: 值），不是 {type(raw).__name__}")
    return validate(raw, kind, path)


def validate(raw: dict, kind: str, where: str = "<設定>") -> Strict:
    """驗證一份已經讀成 dict 的設定（API、測試用），回傳那一種的模型；where 只用在錯誤訊息。
    `_` 開頭的鍵是 yaml 的 anchor（`_tx: &tx …`），只給 yaml 自己用，驗證前拿掉。"""
    model = model_of(kind)
    body = {k: v for k, v in raw.items() if not str(k).startswith("_")}
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise ConfigError("\n".join(_format(e, model, where))) from None


# ── 錯誤訊息 ──

def _format(e: ValidationError, model: type[BaseModel], where: str) -> list[str]:
    seen, out = set(), []
    for err in e.errors():
        path, fields = _resolve(model, err["loc"])
        if err["type"] == "extra_forbidden":
            msg = f"沒有這個欄位；這一層能寫的是 {'、'.join(fields)}" if fields else "沒有這個欄位"
        elif err["type"] == "missing":
            msg = "少了這個欄位（必填）"
        else:
            given = err.get("input")
            msg = err["msg"].removeprefix("Value error, ") + (f"（給的是 {given!r}）" if given is not None and not isinstance(given, (dict, list)) else "")
        # union 的每個分支各報一次（int 不行、float 也不行…）：型別錯的同一個位置只留第一條
        key = (path, "type") if err["type"].endswith(("_parsing", "_type")) else (path, msg)
        if key not in seen:
            seen.add(key)
            out.append(f"{where}: {path or '（最上層）'}：{msg}")
    return out


def _resolve(model: type[BaseModel], loc: tuple) -> tuple[str, list[str]]:
    """pydantic 的 loc（('checks', 0, 'min_peer')）→ 人看的路徑（checks[0].min_peer）和那一層模型的欄位清單。
    union 分支的 tag（'int'、'dict[str,any]'…）走不下去就停，路徑只到走得到的地方。"""
    cur: Any = model
    parts: list[str] = []
    fields: list[str] = []
    for p in loc:
        members = _members(cur)
        if len(members) > 1:                       # union：這一段是分支的 tag（int、dict[str,any]、Pick…），不是鍵
            cur = _pick_member(members, str(p))
            if cur is None:
                break
            continue
        cur = members[0] if members else None
        if isinstance(p, int):
            if typing.get_origin(cur) not in (list, tuple) or not typing.get_args(cur):
                break
            parts.append(f"[{p}]")
            cur = typing.get_args(cur)[0]
            continue
        if isinstance(cur, type) and issubclass(cur, BaseModel):
            fields = [f.alias or n for n, f in cur.model_fields.items()]
            hit = next((f for n, f in cur.model_fields.items() if p in (n, f.alias)), None)
            parts.append(f".{p}" if parts else str(p))
            if hit is None:        # extra_forbidden：這個鍵不在模型裡
                break
            cur = hit.annotation
            continue
        if typing.get_origin(cur) is dict and len(typing.get_args(cur)) == 2:
            parts.append(f".{p}" if parts else str(p))
            cur = typing.get_args(cur)[1]
            continue
        break
    return "".join(parts), fields


def _members(ann) -> list:
    """Optional／Union／Annotated 拆開成一個個型別（None 不算）。"""
    if typing.get_origin(ann) is typing.Annotated:
        return _members(typing.get_args(ann)[0])
    if isinstance(ann, types.UnionType) or typing.get_origin(ann) is typing.Union:
        return [m for a in typing.get_args(ann) if a is not type(None) for m in _members(a)]
    return [ann]


def _pick_member(members: list, tag: str):
    """pydantic 在 loc 裡標的 union 分支（'int'、'list[SeverityClass]'、'Pick'、'literal[...]'）→ 對應的型別。"""
    tag = tag.lower()
    for m in members:
        name = (m.__name__ if isinstance(m, type) else getattr(typing.get_origin(m), "__name__", str(m))).lower()
        if tag == name or tag.startswith(name + "["):
            return m
    return None

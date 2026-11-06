"""設定檔的載入與驗證（pydantic）。指令只透過 load_config 讀 yaml，不自己 yaml.safe_load。

load_config(path, kind)：讀 yaml → 用 kind 對應的模型（core/config_models.MODELS）驗證 → 倒回 dict。
回傳的 dict 內容跟 yaml 一樣（exclude_unset：不塞預設值；各指令的 cfg.get(..., 預設) 照舊）；
最上層 `_` 開頭的鍵（yaml 的 anchor：_svc、_ns…）不驗、原樣留著。
鍵打錯（例如 min_peer）、型別不對、範圍不對 → ConfigError（ValueError），訊息有檔名、欄位路徑、這一層能寫的欄位。"""
from __future__ import annotations

import types
import typing
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from aiops.config.models import MODELS

KINDS = ("series", "rules", "peer", "quality", "trace", "rca", "schedule", "chronic", "prune", "calendar", "adjustments", "ingest", "paths")


class ConfigError(ValueError):
    """設定檔不對。str() 是一行一個問題：<檔名>: <欄位路徑>：<問題>。"""


def load_config(path: str, kind: str) -> dict:
    """讀一份設定檔。kind 是哪一種（KINDS），用它挑模型驗證。回傳 dict，鍵值跟 yaml 一樣。"""
    if kind not in KINDS:
        raise ValueError(f"不認得的設定種類 {kind!r}，只能是 {'、'.join(KINDS)}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 最上層要是 mapping（鍵: 值），不是 {type(raw).__name__}")
    return validate(raw, kind, path)


def validate(raw: dict, kind: str, where: str = "<設定>") -> dict:
    """驗證一份已經讀成 dict 的設定（API、測試用）。回傳的 dict 跟 raw 內容一樣；where 只用在錯誤訊息。"""
    model = MODELS[kind]
    body = {k: v for k, v in raw.items() if not str(k).startswith("_")}
    try:
        checked = model.model_validate(body).model_dump(exclude_unset=True, by_alias=True)
    except ValidationError as e:
        raise ConfigError("\n".join(_format(e, model, where))) from None
    # 依 yaml 原本的順序組回去；_ 開頭的 anchor 原樣
    return {k: checked.get(k, v) for k, v in raw.items()}


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

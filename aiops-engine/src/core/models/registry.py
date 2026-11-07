"""Registry：名字 → 值的登記表（對應 @superset-ui/core 的 models/Registry.ts）。

  register_value(key, value)     立刻有值
  register_loader(key, loader)   延遲載入：第一次 get 才呼叫 loader，結果記起來
重名照 OverwritePolicy：ALLOW 蓋掉、WARN 蓋掉並印一行、PROHIBIT 丟 ValueError。
"""
from __future__ import annotations

import enum
import sys
from collections.abc import Callable
from typing import Generic, TypeVar

V = TypeVar("V")


class OverwritePolicy(enum.Enum):
    ALLOW = "ALLOW"
    WARN = "WARN"
    PROHIBIT = "PROHIBIT"


class Registry(Generic[V]):
    def __init__(self, name: str = "", overwrite_policy: OverwritePolicy = OverwritePolicy.ALLOW):
        self.name = name
        self.overwrite_policy = overwrite_policy
        self._items: dict[str, tuple[bool, object]] = {}     # key → (是 loader 嗎, 值或 loader)

    def clear(self) -> "Registry[V]":
        self._items.clear()
        return self

    def has(self, key: str) -> bool:
        return key in self._items

    def _check_overwrite(self, key: str) -> None:
        if key not in self._items:
            return
        if self.overwrite_policy is OverwritePolicy.PROHIBIT:
            raise ValueError(f'Registry "{self.name}" 已經有 "{key}"（OverwritePolicy.PROHIBIT）')
        if self.overwrite_policy is OverwritePolicy.WARN:
            print(f'Registry "{self.name}" 的 "{key}" 被蓋掉', file=sys.stderr)

    def register_value(self, key: str, value: V) -> "Registry[V]":
        self._check_overwrite(key)
        self._items[key] = (False, value)
        return self

    def register_loader(self, key: str, loader: Callable[[], V]) -> "Registry[V]":
        self._check_overwrite(key)
        self._items[key] = (True, loader)
        return self

    def get(self, key: str) -> V | None:
        """有這個 key 回值（loader 第一次在這裡跑）；沒有回 None。"""
        if key not in self._items:
            return None
        is_loader, item = self._items[key]
        if is_loader:
            item = item()
            self._items[key] = (False, item)
        return item

    def keys(self) -> list[str]:
        return list(self._items)

    def values(self) -> list[V]:
        return [self.get(k) for k in self._items]

    def entries(self) -> list[tuple[str, V]]:
        return [(k, self.get(k)) for k in self._items]

    def remove(self, key: str) -> "Registry[V]":
        self._items.pop(key, None)
        return self

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: str) -> bool:
        return key in self._items

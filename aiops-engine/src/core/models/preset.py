"""Preset（對應 @superset-ui/core 的 models/Preset.ts）：一包 plugin（和別的 preset）一起登記。
register 先登記底下的 preset、再登記自己的 plugin（照給的順序）；unregister 反過來。"""
from __future__ import annotations

from collections.abc import Iterable

from core.models.plugin import Plugin


class Preset:
    def __init__(self, name: str = "", description: str = "", presets: Iterable["Preset"] = (), plugins: Iterable[Plugin] = ()):
        self.name = name
        self.description = description
        self.presets = list(presets)
        self.plugins = list(plugins)

    def register(self) -> "Preset":
        for preset in self.presets:
            preset.register()
        for plugin in self.plugins:
            plugin.register()
        return self

    def unregister(self) -> "Preset":
        for plugin in self.plugins:
            plugin.unregister()
        for preset in self.presets:
            preset.unregister()
        return self

"""Plugin（對應 @superset-ui/core 的 models/Plugin.ts）：config 加 configure／reset_config／register／unregister。

    XxxPipelinePlugin().configure({"key": "golden_signals"}).register()
"""
from __future__ import annotations


class Plugin:
    def __init__(self):
        self.config: dict = {}

    def reset_config(self) -> "Plugin":
        self.config = {}
        return self

    def configure(self, config: dict, replace: bool = False) -> "Plugin":
        """合併（replace=True 整個換掉）config，回傳 self 讓呼叫串起來。"""
        self.config = dict(config) if replace else {**self.config, **config}
        return self

    def register(self) -> "Plugin":
        return self

    def unregister(self) -> "Plugin":
        return self

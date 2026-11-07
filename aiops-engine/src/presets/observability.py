"""observability（IT 可觀測性）的 Preset：目前就是 MainPreset（全部內建外掛都登記，用哪幾個由情境資料檔的 blocks: 選）。
情境要少登記某些外掛時在這裡改 plugins。"""
from __future__ import annotations

from core import Preset

from presets.main_preset import MainPreset


class ObservabilityPreset(Preset):
    def __init__(self):
        super().__init__(name="observability", description="IT 可觀測性", presets=[MainPreset()])

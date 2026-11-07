"""PipelineProps（對應 @superset-ui/core 的 chart/models/ChartProps.ts）：主程式交給外掛的東西。
form_data 是這個外掛的參數（control panel 補過預設）；data 是這一段準備好的資料（每一段不同，見 common/stages.py 的 contract）。
外掛的 transform_props 把它整理成 run 要的樣子。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PipelineProps:
    form_data: dict = field(default_factory=dict)
    data: object = None

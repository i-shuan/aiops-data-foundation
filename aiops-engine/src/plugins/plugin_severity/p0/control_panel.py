"""p0 的 control panel（對應 controlPanel.tsx）。實際的值照舊從 rca.yaml 的 alerts.p0_services 讀。"""
from __future__ import annotations

from core import ControlConfig, ControlPanelConfig, ControlPanelSectionConfig

control_panel = ControlPanelConfig(control_panel_sections=[
    ControlPanelSectionConfig(label="P0 服務", expanded=True, control_set_rows=[
        [{"name": "p0_services", "config": ControlConfig(type="ListControl", default=[], label="服務名（完全相等或整個名字對得上 regex）")}],
    ]),
])

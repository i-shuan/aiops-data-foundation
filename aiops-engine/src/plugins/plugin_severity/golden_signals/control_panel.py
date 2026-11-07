"""golden_signals 的 control panel（對應 controlPanel.tsx）。預設值跟 incident/alert_severity.GradeOptions 一樣（tests 鎖著）；
實際的值照舊從 rca.yaml 的 alerts.severity 讀。"""
from __future__ import annotations

from core import ControlConfig, ControlPanelConfig, ControlPanelSectionConfig

control_panel = ControlPanelConfig(control_panel_sections=[
    ControlPanelSectionConfig(label="持續多久才算", expanded=True, control_set_rows=[
        [{"name": "window", "config": ControlConfig(type="NumberControl", default=15, min=1, label="滑動窗（分鐘）：看最後這段裡有幾分鐘有異常")},
         {"name": "min_coverage", "config": ControlConfig(type="NumberControl", default=0.8, min=0, max=1, label="窗內有異常的分鐘比例 ≥ 這個才算持續")}],
        [{"name": "min_duration", "config": ControlConfig(type="NumberControl", default=10, min=0, label="持續至少幾分鐘（第一筆到最後一筆）")}],
    ]),
    ControlPanelSectionConfig(label="幾類訊號才升級", control_set_rows=[
        [{"name": "min_span_critical", "config": ControlConfig(type="NumberControl", default=3, min=0, label="≥ 3 類要升 critical，至少跨這麼多分鐘")},
         {"name": "min_points_per_class", "config": ControlConfig(type="NumberControl", default=2, min=1, label="一類至少幾筆異常才算一個維度")}],
    ]),
    ControlPanelSectionConfig(label="換版", control_set_rows=[
        [{"name": "deploy_grace", "config": ControlConfig(type="NumberControl", default=10, min=0, label="換版後幾分鐘內的異常上限 warning")}],
    ]),
])

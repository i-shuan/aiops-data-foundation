"""共用的控制項（對應 @superset-ui/chart-controls 的 sharedControls）：好幾個外掛都用得到的參數放這裡，外掛的 control_set_rows 寫名字就好。"""
from __future__ import annotations

from core.controls.types import ControlConfig, shared_controls

shared_controls.update({
    "lookback": ControlConfig(type="TextControl", default="30m", label="讀最近多久的資料（例：30m、2h）"),
    "min_events": ControlConfig(type="NumberControl", default=20, min=0, label="事件數不到的不判斷"),
})

"""Control panel 的型別（對應 @superset-ui/chart-controls 的 ControlPanelConfig、ControlPanelSectionConfig、control 的設定）。

Superset 的外掛用 control_panel_sections／control_set_rows 宣告有哪些控制項、畫面照它長出來；
我們沒有畫面，宣告拿來：`main detector blocks` 印出可調參數、build_form_data 補預設＋轉型＋驗證（打錯字當場擋）。

    ControlPanelConfig(control_panel_sections=[
        ControlPanelSectionConfig(label="持續多久才算", expanded=True, control_set_rows=[
            ["window", "min_coverage"],                                       # 字串＝shared_controls 裡的名字
            [{"name": "min_duration", "config": ControlConfig(type="NumberControl", default=10, label="持續至少幾分鐘")}],
        ]),
    ])
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

CONTROL_TYPES = ("TextControl", "TextAreaControl", "NumberControl", "SliderControl", "CheckboxControl", "SelectControl", "ListControl")


@dataclass(frozen=True)
class ControlConfig:
    """一個控制項。type 照 Superset 的名字；ListControl 是我們加的（多值，例如 p0_services）。"""
    type: str = "TextControl"
    label: str = ""
    description: str = ""
    default: object = None
    choices: tuple = ()
    min: float | None = None
    max: float | None = None

    def __post_init__(self):
        if self.type not in CONTROL_TYPES:
            raise ValueError(f"control 的 type 要是 {'、'.join(CONTROL_TYPES)} 之一，不是 {self.type!r}")

    def coerce(self, name: str, value):
        """使用者給的值 → 這個控制項的型別；不對丟 ValueError（講出是哪個參數）。None 回 default。"""
        if value is None:
            return self.default
        try:
            if self.type in ("NumberControl", "SliderControl"):
                if isinstance(value, bool):
                    raise TypeError
                out = float(value) if isinstance(self.default, float) or "." in str(value) else int(value)
                if (self.min is not None and out < self.min) or (self.max is not None and out > self.max):
                    raise ValueError(f"要在 {self.min}～{self.max} 之間")
                return out
            if self.type == "CheckboxControl":
                if isinstance(value, bool):
                    return value
                raise TypeError
            if self.type == "ListControl":
                if isinstance(value, (list, tuple)):
                    return [str(x) for x in value]
                raise TypeError
            if self.type == "SelectControl":
                if self.choices and value not in self.choices:
                    raise ValueError(f"要是 {', '.join(map(str, self.choices))} 之一")
                return value
            return str(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"參數 {name} 不對（{self.type}）：{value!r}" + (f"，{e}" if str(e) else "")) from None


ControlSetItem = str | dict          # 字串（shared_controls 的名字）或 {"name": …, "config": ControlConfig}

# 共用的控制項（Superset 的 sharedControls）：內容在 shared_controls.py 填進來（core/controls/__init__.py 會 import 它）
shared_controls: dict[str, ControlConfig] = {}


@dataclass
class ControlPanelSectionConfig:
    label: str = ""
    description: str = ""
    expanded: bool = False
    control_set_rows: list[list[ControlSetItem]] = field(default_factory=list)


@dataclass
class ControlPanelConfig:
    control_panel_sections: list[ControlPanelSectionConfig] = field(default_factory=list)
    control_overrides: dict[str, ControlConfig] = field(default_factory=dict)
    form_data_overrides: Callable[[dict], dict] | None = None

    def controls(self) -> dict[str, ControlConfig]:
        """全部控制項 {名字: 設定}，順序照 sections → rows。"""
        out: dict[str, ControlConfig] = {}
        for section in self.control_panel_sections:
            for row in section.control_set_rows:
                for item in row:
                    name, control = (item["name"], item.get("config")) if isinstance(item, dict) else (item, None)
                    control = self.control_overrides.get(name) or control or shared_controls.get(name)
                    if control is None:
                        raise ValueError(f"控制項 {name} 沒有定義：寫成 {{'name': …, 'config': ControlConfig(...)}} 或放進 shared_controls")
                    out[name] = control
        return out

    def build_form_data(self, raw: dict | None = None) -> dict:
        """raw（使用者給的 {參數: 值}）→ form_data：補預設、轉型、驗證；不認得的參數名講出有哪些。"""
        raw = dict(raw or {})
        controls = self.controls()
        unknown = [k for k in raw if k not in controls]
        if unknown:
            raise ValueError(f"不認得的參數 {'、'.join(map(str, unknown))}；可調的有：{'、'.join(controls) or '（沒有）'}")
        data = {name: control.coerce(name, raw.get(name)) for name, control in controls.items()}
        return self.form_data_overrides(data) if self.form_data_overrides else data

    def describe(self) -> list[str]:
        return [f"{name} ({c.type}, 預設 {c.default!r})" + (f"：{c.label or c.description}" if (c.label or c.description) else "")
                for name, c in self.controls().items()]

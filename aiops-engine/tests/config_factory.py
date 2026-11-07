"""測試用：一行造出型別化的設定（不用每個測試自己寫整份 yaml）。"""
from common.config import validate
from incident.config.rca import Alerts, RcaConfig  # noqa: F401  測試直接用 Alerts(...)

BASE_INDICES = {"traces": "traces-*", "logs": "logs-*"}


def rca(**over) -> RcaConfig:
    """rca.yaml：沒給 indices 就用 BASE_INDICES；其他欄位照 yaml 的寫法（alerts={...}、timezone=...）。"""
    return validate({"indices": BASE_INDICES, **over}, "rca")


def rule_of(d: dict):
    """rules.yaml 的一條規則：沒給的必填欄位（name、index、by、threshold，以及依 type 要的 field／other／den）補一個不影響測試的值。"""
    from detect.config.rules import Rule
    d = {"name": "r", "index": "m", "by": ["service"], "threshold": 1, **d}
    t = d.get("type")
    if t == "window":
        d.setdefault("value", "count")
    else:
        d.setdefault("field", "metrics.x")
    if t == "compare":
        d.setdefault("other", "metrics.y")
    if t in ("ratio", "increase_ratio"):
        d.setdefault("den", ["metrics.d"])
    return Rule.model_validate(d)


def series_of(d: dict):
    """series.yaml 的一條序列：沒給的必填欄位（index、by、value）補一個不影響測試的值。"""
    from detect.config.series import Series
    return Series.model_validate({"index": "m", "by": ["service"], "value": "count", **d})

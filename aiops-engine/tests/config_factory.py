"""測試用：一行造出型別化的設定（不用每個測試自己寫整份 yaml）。"""
from common.config import validate
from incident.config.rca import Alerts, RcaConfig  # noqa: F401  測試直接用 Alerts(...)

BASE_INDICES = {"anomalies": "aiops-anomalies", "traces": "traces-*", "logs": "logs-*"}


def rca(**over) -> RcaConfig:
    """rca.yaml：沒給 indices 就用 BASE_INDICES；其他欄位照 yaml 的寫法（alerts={...}、timezone=...）。"""
    return validate({"indices": BASE_INDICES, **over}, "rca")

"""⑥ 一件的嚴重度：共用的部分（訊號分類、參數、外掛收到的 SeverityContext）。
怎麼評在外掛：plugins/plugin_severity/golden_signals（幾類黃金訊號、持續、換版上限）、p0（P0 服務升一級）。
（docs/pdca/meta/2026-11-severity-grading.md）

  critical  靜態臨界值規則 critical；或 ≥ 3 類黃金訊號同時異常、不是單點
  high      2 類黃金訊號；或 1 類但持續（滑動窗覆蓋率夠）；或 1 類但好幾條（幾支 API 的錯誤率都跳）
  warning   單點、單一序列、持續很短；換版後 N 分鐘內（上限鎖在 warning，過了 N 分鐘還沒收斂就照上面重評）

黃金訊號的「類」看序列名：latency（延遲）、errors（錯誤）、downstream（往外呼叫：DB、下游、外部系統）、
saturation（飽和：CPU、記憶體、連線池、lag）、traffic（流量）。
廣度算「幾類」，不是幾條：3 支 API 的錯誤率都跳，仍是 errors 一類。一類至少要 min_points_per_class 筆才算（一筆零星的不算一個維度）。
traffic 不算廣度（流量變了不一定是壞事，而且什麼事故都會帶動流量），只在 why 裡提。
換版上限看「最後一筆異常離換版多久」，不看現在：換版後 N 分鐘內的異常上限 warning；N 分鐘後還有異常就照常評（壞版起不來不會被永遠靜音）。
Grafana 只通知 critical、high；warning 只進事故清單（grafana/helmchart/files/policies.yaml）。

P0 服務（rca.yaml 的 alerts.p0_services）：上面算完再升一級（escalate）。換版後的上限 warning 也跟著變 high：P0 換版壞了也要叫人。
門檻（算不算異常）不動：P0 要更早報，用靜態臨界值規則或 confirm_points: 1（docs/pdca/meta/2026-11-cases-tier-drop.md）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

CLASSES = (   # 依序比對：流量先（dep.calls 是流量，不是下游變慢）、再往外呼叫、延遲、錯誤、飽和
    ("traffic", (r"requests", r"\.calls$", r"\.lines$", r"\.count$")),
    ("downstream", (r"^dep\.", r"destination", r"downstream", r"upstream", r"external")),
    ("latency", (r"latency", r"slow_ratio", r"duration", r"p9\d")),
    ("errors", (r"error", r"exception", r"swallow", r"restart", r"failed", r"not_ready", r"pending")),
    ("saturation", (r"cpu", r"memory", r"disk", r"pool", r"lag", r"throttl", r"heap", r"\bgc\b", r"thread", r"full")),
)
RANK = {"warning": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class GradeOptions:
    window: int = 15          # 滑動窗（分鐘）：看最後這段裡有幾分鐘有異常
    min_coverage: float = 0.8  # 窗內有異常的分鐘比例 ≥ 這個才算「持續」（毛刺掉回去一分鐘不會重算）
    min_duration: int = 10    # 持續至少幾分鐘（第一筆到最後一筆）
    min_span_critical: int = 3  # ≥ 3 類要升 critical，至少要跨這麼多分鐘（單點不算）
    min_points_per_class: int = 2  # 一類至少幾筆異常才算一個維度
    deploy_grace: int = 10    # 換版後幾分鐘內的異常上限 warning


def parse_classes(raw) -> tuple | None:
    """rca.yaml 的 alerts.severity.classes → 跟 CLASSES 同一種格式；沒設回 None（用內建的）。
    寫法：{類名: [pattern, …]}（依序比對）或 [{name, patterns}]。工廠版用它把序列分成 pressure、temperature、metrology…（參數家族）。"""
    if not raw:
        return None
    if isinstance(raw, dict):
        return tuple((str(k), tuple(str(p) for p in (v if isinstance(v, list) else [v]))) for k, v in raw.items())
    return tuple((str(c["name"]), tuple(str(p) for p in c.get("patterns") or [])) for c in raw)


def signal_class(series: str, classes: tuple | None = None) -> str:
    """序列名 → 類；規則、同儕的前綴（rule.、peer.）去掉再看。不認得的是 other（不算廣度）。classes：自訂的分類表（parse_classes）。"""
    s = re.sub(r"^(rule|peer|quality)\.", "", str(series or "")).lower()
    for cls, pats in (classes or CLASSES):
        if any(re.search(p, s) for p in pats):
            return cls
    return "other"


# ── 外掛收到的東西（plugins/plugin_severity 的 run(ctx, g)）──

@dataclass
class SeverityContext:
    """一件要評嚴重度時手上的東西（incident/alert_service.grade_alert 準備）。自己寫的嚴重度外掛從這裡拿。"""
    alert: object                       # Alert
    series: list[str]                   # 這件自己服務的異常序列名
    timestamps: list[datetime]          # 每筆異常的時間
    members: int                        # 不同的（序列, 對象）數（只算黃金訊號）
    rule_critical: bool                 # 有靜態規則 critical 的異常
    now: datetime
    deploy_at: datetime | None
    opts: GradeOptions
    classes: tuple | None
    alerts_config: object               # rca.yaml 的 alerts（p0_services 等）


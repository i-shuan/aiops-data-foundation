"""⑥ 一件的嚴重度：不是偏離多少倍，是要不要吵人（docs/pdca/meta/2026-11-severity-grading.md）。

  critical  靜態臨界值規則 critical；或 ≥ 3 類黃金訊號同時異常、不是單點
  high      2 類黃金訊號；或 1 類但持續（滑動窗覆蓋率夠）；或 1 類但好幾條（幾支 API 的錯誤率都跳）
  warning   單點、單一序列、持續很短；換版後 N 分鐘內（上限鎖在 warning，過了 N 分鐘還沒收斂就照上面重評）

訊號的「類」看序列名，分類表由 plugin 給（core/plugin.py 的 severity_classes：IT 的黃金訊號 latency／errors／downstream／saturation／traffic 在
plugins/observability/profiles.py，工廠的參數家族在 plugins/factory）、cfg 的 alerts.severity.classes 可覆蓋；沒有分類表就全部 other、廣度 0。
廣度算「幾類」，不是幾條：3 支 API 的錯誤率都跳，仍是 errors 一類。一類至少要 min_points_per_class 筆才算（一筆零星的不算一個維度）。
traffic 不算廣度（流量變了不一定是壞事，而且什麼事故都會帶動流量），只在 why 裡提。
換版上限看「最後一筆異常離換版多久」，不看現在：換版後 N 分鐘內的異常上限 warning；N 分鐘後還有異常就照常評（壞版起不來不會被永遠靜音）。
Grafana 只通知 critical、high；warning 只進事故清單（grafana/helmchart/files/policies.yaml）。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

RANK = {"warning": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class GradeOptions:
    window: int = 15          # 滑動窗（分鐘）：看最後這段裡有幾分鐘有異常
    min_coverage: float = 0.8  # 窗內有異常的分鐘比例 ≥ 這個才算「持續」（毛刺掉回去一分鐘不會重算）
    min_duration: int = 10    # 持續至少幾分鐘（第一筆到最後一筆）
    min_span_critical: int = 3  # ≥ 3 類要升 critical，至少要跨這麼多分鐘（單點不算）。rca.yaml 的 key，名字照舊
    min_points_per_class: int = 2  # 一類至少幾筆異常才算一個維度
    deploy_grace: int = 10    # 換版後幾分鐘內的異常上限 warning


def parse_classes(raw) -> tuple | None:
    """rca.yaml 的 alerts.severity.classes（或 plugin 的 severity_classes）→ ((類名, (pattern, …)), …)，依序比對；沒設回 None（不分類）。
    寫法：{類名: [pattern, …]} 或 [{name, patterns}]。IT 是黃金訊號（plugins/observability/profiles.py）、工廠是參數家族（pressure、temperature…）。"""
    if not raw:
        return None
    if isinstance(raw, dict):
        return tuple((str(k), tuple(str(p) for p in (v if isinstance(v, list) else [v]))) for k, v in raw.items())
    return tuple((str(c["name"]), tuple(str(p) for p in c.get("patterns") or [])) for c in raw)


def signal_class(series: str, classes: tuple | None = None) -> str:
    """序列名 → 類；規則、同儕的前綴（rule.、peer.）去掉再看。不認得的是 other（不算廣度）。classes：分類表（parse_classes），None＝全部 other。"""
    s = re.sub(r"^(rule|peer|quality)\.", "", str(series or "")).lower()
    for cls, pats in (classes or ()):
        if any(re.search(p, s) for p in pats):
            return cls
    return "other"


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def coverage(timestamps: list[datetime], window: int) -> tuple[float, int]:
    """(最後 window 分鐘內有異常的分鐘比例, 第一筆到最後一筆的分鐘數)。"""
    if not timestamps:
        return 0.0, 0
    mins = {_minute(t) for t in timestamps}
    last, first = max(mins), min(mins)
    spread = int((last - first).total_seconds() // 60)
    lo = last - timedelta(minutes=window - 1)
    covered = sum(1 for m in mins if m >= lo)
    slots = min(window, spread + 1)
    return covered / slots, spread


def grade(series: list[str], timestamps: list[datetime], *, members: int = 1, rule_critical: bool = False,
          now: datetime | None = None, deploy_at: datetime | None = None, opts: GradeOptions = GradeOptions(),
          classes: tuple | None = None) -> dict:
    """series：這件的異常序列名；timestamps：每筆異常的時間；members：不同的（序列, 對象）數；
    rule_critical：有靜態規則 critical 的異常；deploy_at：這個服務最近一次換版的時間（沒有就 None）；
    classes：分類表（parse_classes），沒給就不分類（全部 other）。
    回傳 {"severity", "why", "classes", "breadth", "span"（第一筆到最後一筆幾分鐘）, "coverage", "sustained", "capped"}。"""
    counts = Counter(signal_class(s, classes) for s in series)
    classes = {c for c, n in counts.items() if c != "other" and n >= opts.min_points_per_class}
    golden = sorted(classes - {"traffic"})
    breadth = len(golden)
    label = "、".join(golden) or "、".join(sorted(classes)) or "—"
    cov, spread = coverage(timestamps, opts.window)
    sustained = spread >= opts.min_duration and cov >= opts.min_coverage
    if rule_critical:
        sev, why = "critical", "靜態規則 critical"
    elif breadth >= 3 and spread >= opts.min_span_critical:
        sev, why = "critical", f"{breadth} 類訊號同時異常（{label}），跨 {spread} 分鐘"
    elif breadth >= 2 or (breadth == 1 and (sustained or members >= 2)):
        what = f"{breadth} 類訊號（{label}）" if breadth >= 2 else \
            (f"單一類（{label}）但持續 {spread} 分鐘、窗內 {cov:.0%} 有異常" if sustained else f"單一類（{label}）但 {members} 條序列都跳")
        sev, why = "high", what + ("；單點，不升 critical" if breadth >= 3 else "")
    else:
        sev, why = "warning", (f"單一類（{label}）、{spread + 1} 分鐘、沒持續" if golden else "只有流量、零星或不認得的序列")
    capped = False
    last = max(timestamps) if timestamps else now
    if deploy_at is not None and last is not None and timedelta(0) <= last - deploy_at < timedelta(minutes=opts.deploy_grace) and RANK[sev] > 1:
        capped = True
        why = f"換版後 {int((last - deploy_at).total_seconds() // 60)} 分鐘內（{opts.deploy_grace} 分鐘內上限 warning；原本 {sev}：{why}）"
        sev = "warning"
    return {"severity": sev, "why": why, "classes": sorted(classes), "breadth": breadth, "span": spread, "coverage": round(cov, 2),
            "sustained": sustained, "capped": capped}

"""golden_signals：幾類黃金訊號、持續多久、換版後上限 warning（原本在 incident/alert_severity.py）。

  critical  靜態臨界值規則 critical；或 ≥ 3 類黃金訊號同時異常、不是單點
  high      2 類黃金訊號；或 1 類但持續（滑動窗覆蓋率夠）；或 1 類但好幾條（幾支 API 的錯誤率都跳）
  warning   單點、單一序列、持續很短；換版後 N 分鐘內（上限鎖在 warning）
訊號分類、參數（GradeOptions）、外掛收到的 SeverityContext 在 incident/alert_severity.py（主程式也用）。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from incident.alert_severity import RANK, GradeOptions, SeverityContext, signal_class


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def coverage(timestamps: list[datetime], window: int) -> tuple[float, int]:
    """(最後 window 分鐘內有異常的分鐘比例, 第一筆到最後一筆的分鐘數)。"""
    if not timestamps:
        return 0.0, 0
    mins = {_minute(t) for t in timestamps}
    last, first = max(mins), min(mins)
    span = int((last - first).total_seconds() // 60)
    lo = last - timedelta(minutes=window - 1)
    covered = sum(1 for m in mins if m >= lo)
    slots = min(window, span + 1)
    return covered / slots, span


def grade(series: list[str], timestamps: list[datetime], *, members: int = 1, rule_critical: bool = False,
          now: datetime | None = None, deploy_at: datetime | None = None, opts: GradeOptions = GradeOptions(),
          classes: tuple | None = None) -> dict:
    """series：這件的異常序列名；timestamps：每筆異常的時間；members：不同的（序列, 對象）數；
    rule_critical：有靜態規則 critical 的異常；deploy_at：這個服務最近一次換版的時間（沒有就 None）；
    classes：自訂的分類表（parse_classes），沒給用 CLASSES。
    回傳 {"severity", "why", "classes", "breadth", "span", "coverage", "sustained", "capped"}。"""
    counts = Counter(signal_class(s, classes) for s in series)
    classes = {c for c, n in counts.items() if c != "other" and n >= opts.min_points_per_class}
    golden = sorted(classes - {"traffic"})
    breadth = len(golden)
    label = "、".join(golden) or "、".join(sorted(classes)) or "—"
    cov, span = coverage(timestamps, opts.window)
    sustained = span >= opts.min_duration and cov >= opts.min_coverage
    if rule_critical:
        sev, why = "critical", "靜態規則 critical"
    elif breadth >= 3 and span >= opts.min_span_critical:
        sev, why = "critical", f"{breadth} 類訊號同時異常（{label}），跨 {span} 分鐘"
    elif breadth >= 2 or (breadth == 1 and (sustained or members >= 2)):
        what = f"{breadth} 類訊號（{label}）" if breadth >= 2 else \
            (f"單一類（{label}）但持續 {span} 分鐘、窗內 {cov:.0%} 有異常" if sustained else f"單一類（{label}）但 {members} 條序列都跳")
        sev, why = "high", what + ("；單點，不升 critical" if breadth >= 3 else "")
    else:
        sev, why = "warning", (f"單一類（{label}）、{span + 1} 分鐘、沒持續" if golden else "只有流量、零星或不認得的序列")
    capped = False
    last = max(timestamps) if timestamps else now
    if deploy_at is not None and last is not None and timedelta(0) <= last - deploy_at < timedelta(minutes=opts.deploy_grace) and RANK[sev] > 1:
        capped = True
        why = f"換版後 {int((last - deploy_at).total_seconds() // 60)} 分鐘內（{opts.deploy_grace} 分鐘內上限 warning；原本 {sev}：{why}）"
        sev = "warning"
    return {"severity": sev, "why": why, "classes": sorted(classes), "breadth": breadth, "span": span, "coverage": round(cov, 2),
            "sustained": sustained, "capped": capped}


def run(ctx: SeverityContext, g: dict | None) -> dict:
    """內建的第一塊：幾類黃金訊號、持續多久、換版後上限 warning（grade）。"""
    return grade(ctx.series, ctx.timestamps, members=ctx.members, rule_critical=ctx.rule_critical, now=ctx.now,
                 deploy_at=ctx.deploy_at, opts=ctx.opts, classes=ctx.classes)

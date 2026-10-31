"""5 欄 cron（分 時 日 月 週）的比對：* 、*/n、a-b、a-b/n、a,b,c；週 0～7（0 和 7 都是星期日）。
只做「這一分鐘該不該跑」，不算下一次是幾點（排程器每分鐘醒來問一次就夠）。日和週都有限制時兩個都要符合（跟 K8s CronJob 的
「任一符合」不同；這裡的排程表沒有同時寫日和週的）。"""
from __future__ import annotations

from datetime import datetime

RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
NAMES = ("分", "時", "日", "月", "週")


def _field(expr: str, lo: int, hi: int, name: str) -> set[int]:
    out: set[int] = set()
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step < 1:
                raise ValueError(f"cron 的{name}欄步進要 ≥ 1：{expr}")
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
            if step != 1 and "/" in expr:   # "5/10" 這種：從 5 起每 10
                b = hi
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"cron 的{name}欄超出 {lo}～{hi}：{expr}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    def __init__(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"cron 要 5 欄（分 時 日 月 週）：{expr!r}")
        self.expr = expr
        self.minute, self.hour, self.dom, self.month, dow = (
            _field(f, lo, hi, n) for f, (lo, hi), n in zip(fields, RANGES, NAMES))
        self.dow = {0 if d == 7 else d for d in dow}
        self.every_minute = fields[0] == "*"

    def matches(self, t: datetime) -> bool:
        return (t.minute in self.minute and t.hour in self.hour and t.day in self.dom and t.month in self.month
                and (t.weekday() + 1) % 7 in self.dow)     # Python 週一=0 → cron 週日=0

    def __repr__(self) -> str:
        return f"Cron({self.expr!r})"

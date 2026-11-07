"""conclusion：結論（報告的一段；原本是 incident/rca_report.py 的 sec_conclusion）。
報告的骨架（Ctx、標題編號、共用的小工具）在 incident/rca_report.py；這個外掛只寫這一段。"""
from __future__ import annotations

import incident.rca_analyzer as A
from incident.rca_report import Ctx, _ms


def run(c: Ctx) -> None:
    U = c.U
    c.out += ["## 結論（給人確認）", ""]
    if c.top:
        d = c.top[0]
        c.out.append(f"- {c.what} 變慢，主要多在 **{d.segment}**：每個{U}多花 {_ms(d.delta_ms)}（平常 {_ms(d.ref_ms)}）。")
    if c.errs:
        c.out.append(f"- 錯誤最早出在 **{c.errs[0].segment}**（{c.errs[0].now_errors} 個{U.rstrip()}，平常 {c.errs[0].ref_errors}）。")
    new = [p for p in c.pats if p.is_new][:2]
    for p in new:
        c.out.append(f"- 同時間 {p.service} 新出現 log「{p.example.splitlines()[0][:100]}」，{p.now} 次。")
    if c.commons:
        x = c.commons[0]
        if x.is_new:
            c.out.append(f"- 慢的{U} {x.now_share:.0%} 都在平常沒見過的 {x.dim} `{x.value}`：平常那批完全沒有這個值（{c.sc.new_value_hint}），先確認那時有沒有換。")
        else:
            c.out.append(f"- 慢的{U} {x.now_share:.0%} 都是 {x.dim} = `{x.value}`（平常 {x.ref_share:.0%}）：問題可能只在它身上。")
    if c.trend.get("kind") in ("gradual", "sudden"):
        c.out.append("- " + A.describe_trend(c.trend, c.span_label, _ms).split("→ ", 1)[1].replace("**", ""))
    if c.changes:
        x = c.changes[0]
        c.out.append(f"- 出事前 {x['service']} 有變更：{c.local(str(x['ts']))}「{x['message'].splitlines()[0][:80]}」，先確認是不是它。")
    if not (c.top or c.errs or new):
        if c.has_traces is False:
            c.out.append(f"- 沒有{c.TRACE}可以定位哪一段；只看得到 ⑤ 的異常與 log。")
        elif c.has_traces is None:
            c.out.append(f"- 這個設定沒有{c.TRACE}：只看 ⑤ 的異常、log、事故前的變更，定位到哪一段要有{c.TRACE}。")
        else:
            c.out.append(f"- 跟平常比看不出哪一段變化特別大，可能是整體量變大，或問題不在有{c.TRACE}的地方。")

"""logs：新的 log 樣式（報告的一段；原本是 incident/rca_report.py 的 sec_logs）。
報告的骨架（Ctx、標題編號、共用的小工具）在 incident/rca_report.py；這個外掛只寫這一段。"""
from __future__ import annotations

import incident.rca_analyzer as A
from incident.rca_report import Ctx, PAD, _slowest


def run(c: Ctx) -> None:
    logs, ix, U = c.repo.logs, c.ix, c.U
    c.heading("Log：那幾段發生了什麼事")
    c.out += [f"看 {', '.join(c.suspects)} 這段時間的 warn／error，跟{c.ref_label or '平常'}比；新出現的排前面：", ""]
    p_now = logs.problems(ix.logs, c.suspects, c.start, c.end)
    p_ref = logs.problems(ix.logs, c.suspects, c.ref_start, c.ref_end) if c.ref_start else []
    pats = A.compare_patterns(p_now, p_ref)
    c.pats = pats
    c.findings["new_patterns"] = [{"service": p.service, "pattern": p.pattern[:200], "now": p.now, "ref": p.ref, "is_new": bool(p.is_new),
                                   "example": p.example.splitlines()[0][:200]} for p in pats[:8]]
    if pats:
        c.out += [f"| {c.sc.entity} | 樣式 | 現在 | 平常 | 例子 |", "|---|---|---|---|---|"]
        for p in pats[:8]:
            ex = p.example.splitlines()[0][:120].replace("|", "\\|")
            c.out.append(f"| {p.service} | {'🆕 ' if p.is_new else ''}`{p.pattern[:80].replace('|', '/')}` | {p.now} | {p.ref} | {ex} |")
    else:
        c.out.append("沒有比平常多的 warn／error log。")
    sl = _slowest(c) if c.traced else None
    if sl:
        lines = logs.of_traces(ix.logs, [sl[0]["trace_id"]], c.start - PAD, c.end + PAD)
        idl = "lot id" if U.strip() == "lot" else "trace_id"
        c.out += ["", f"最慢那個{U}的 log（依 {idl} 撈出來，{len(lines)} 行）：", "", "```"]
        c.out += [f"{l['ts']}  {l['service']:<14} {str(l['level'] or ''):<5}  {l['message'].splitlines()[0][:150]}" for l in lines[:20]] \
            or [f"（沒有：這幾個{c.sc.entity}的 log 沒寫 {idl}？）"]
        c.out.append("```")
    c.out.append("")

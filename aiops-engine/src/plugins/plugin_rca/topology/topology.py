"""topology：呼叫關係（報告的一段；原本是 incident/rca_report.py 的 sec_topology）。
報告的骨架（Ctx、標題編號、共用的小工具）在 incident/rca_report.py；這個外掛只寫這一段。"""
from __future__ import annotations

import data.store.models as T
from incident.rca_report import Ctx, _skip


def run(c: Ctx) -> None:
    if not c.traced:
        return _skip(c, f"這些{c.U}經過的{c.sc.entity}", f"（沒有{c.TRACE}，略過）")
    edges = T.topology(c.now_tr)
    c.findings["topology"] = [{"caller": e.caller, "callee": e.callee, "calls": e.calls, "errors": e.errors} for e in edges]
    c.heading(f"這些{c.U}經過的{c.sc.entity}")
    c.out += ["```mermaid", T.mermaid(edges), "```", ""]

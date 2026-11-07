"""slowest：最慢的請求的瀑布圖（報告的一段；原本是 incident/rca_report.py 的 sec_slowest）。
報告的骨架（Ctx、標題編號、共用的小工具）在 incident/rca_report.py；這個外掛只寫這一段。"""
from __future__ import annotations

import incident.rca_analyzer as A
from incident.rca_report import Ctx, _skip, _slowest


def run(c: Ctx) -> None:
    U = c.U
    sl = _slowest(c) if c.traced else None
    if not sl:
        return _skip(c, f"最慢的那個{U.strip()}", f"（沒有{c.TRACE}，略過）")
    idl = "lot" if U.strip() == "lot" else "trace_id"
    c.heading(f"最慢的那個{U}（{idl} `{sl[0]['trace_id']}`）")
    c.out += ["```"] + A.waterfall(sl) + ["```", ""]

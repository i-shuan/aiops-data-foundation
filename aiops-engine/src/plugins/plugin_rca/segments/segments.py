"""segments：慢在哪一段、錯從哪開始、共同點（報告的一段；原本是 incident/rca_report.py 的 sec_segments）。
報告的骨架（Ctx、標題編號、共用的小工具）在 incident/rca_report.py；這個外掛只寫這一段。"""
from __future__ import annotations

from incident.config.rca import Commonality
import incident.rca_analyzer as A
from incident.rca_report import Ctx, _delta, _ms, _skip


def run(c: Ctx) -> None:
    """慢在哪一段（每段自己花的時間跟平常比）、錯誤最早出在哪一段、慢的那批的共同點。"""
    U, TRACE = c.U, c.TRACE
    if not c.traced:
        return _skip(c, f"{TRACE}：慢在哪一段", f"（沒有{TRACE}，略過）")
    diffs = A.compare_segments(c.now_tr, c.ref_tr)
    c.heading(f"{TRACE}：慢在哪一段")
    c.out += [f"比較最慢、出錯的 {len(c.now_tr)} 個{U}和平常的 {len(c.ref_tr)} 個{U.rstrip()}，每個{U}在每一段**自己**花的時間"
              "（扣掉等下游的時間，所以慢的那段不會一路算到最上層）：", "",
              f"| 段 | 現在 | 平常 | 多了 | 每個{U}呼叫幾次 |", "|---|---|---|---|---|"]
    for d in diffs[:6]:
        c.out.append(f"| {d.segment} | {_ms(d.now_ms)} | {_ms(d.ref_ms)} | **{_delta(d.delta_ms)}** "
                     f"| {d.now_calls:.1f}（平常 {d.ref_calls:.1f}） |")
    errs = sorted([d for d in diffs if d.now_errors > d.ref_errors], key=lambda d: d.now_errors - d.ref_errors, reverse=True)
    c.diffs, c.errs, c.top = diffs, errs, [d for d in diffs if d.delta_ms > 0][:3]
    c.findings["segments"] = [{"segment": d.segment, "now_ms": d.now_ms, "ref_ms": d.ref_ms, "delta_ms": d.delta_ms,
                               "now_calls": d.now_calls, "ref_calls": d.ref_calls} for d in diffs[:6]]
    c.findings["error_origins"] = [{"segment": d.segment, "now_errors": d.now_errors, "ref_errors": d.ref_errors} for d in errs[:5]]
    if errs:
        c.out += ["", "錯誤最早出在哪一段（上層只是把錯誤往上傳）：", "", "| 段 | 現在 | 平常 |", "|---|---|---|"]
        c.out += [f"| {d.segment} | {d.now_errors} 個{U} | {d.ref_errors} |" for d in errs[:5]]
    c.out.append("")
    # 慢的那批有什麼共同點（平常沒這麼集中）
    common = c.rca_config.commonality
    commons = A.commonality(c.now_tr, c.ref_tr, res_keys=tuple(common.resource) or Commonality().resource,     # 寫空清單＝用預設
                            attr_keys=tuple(common.attrs) or Commonality().attrs,
                            min_share=float(common.min_share), min_lift=float(common.min_lift),
                            min_traces=int(common.min_traces))
    c.commons = commons
    c.findings["commonality"] = [{"dim": x.dim, "value": x.value, "now": x.now, "now_share": x.now_share, "ref_share": x.ref_share}
                                 for x in commons[:5]]
    if commons:
        c.out += [f"這批慢的{U}有什麼共同點（平常沒這麼集中）：", "", f"| 看什麼 | 值 | 慢的{U}裡 | 平常 |", "|---|---|---|---|"]
        c.out += [f"| {x.dim} | `{x.value}` | {x.now_share:.0%}（{x.now} 個） | {'平常沒見過（新的？）' if x.is_new else f'{x.ref_share:.0%}'} |"
                  for x in commons[:5]]
        c.out.append("")
    c.suspects = list(dict.fromkeys([d.segment.split(" › ")[0] for d in c.top + errs[:3]] + [c.service]))

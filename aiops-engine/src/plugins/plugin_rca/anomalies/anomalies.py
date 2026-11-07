"""anomalies：這段時間的異常（報告的一段；原本是 incident/rca_report.py 的 sec_anomalies）。
報告的骨架（Ctx、標題編號、共用的小工具）在 incident/rca_report.py；這個外掛只寫這一段。"""
from __future__ import annotations

from incident.rca_report import Ctx, _trust


def run(c: Ctx) -> None:
    found = c.found
    c.findings["anomalies"] = [{"id": r.get("_id"), "series": r.get("series"), "value": r.get("value"), "median": r.get("median"),
                                "z": r.get("z"), "status": r.get("status"), "severity": r.get("severity"), "at": r.get("@timestamp")}
                               for r in sorted(found, key=lambda r: -abs(r.get("z") or 0))[:10]]
    c.heading("Metrics：發現異常")
    if found:
        c.out += ["| 時間 | 指標 | 哪一段 | 現在 | 平常 | 偏離 | 可信度 |", "|---|---|---|---|---|---|---|"]
        for r in sorted(found, key=lambda r: -abs(r.get("z") or 0))[:10]:
            z = (f"機率 {r['p']:.1e}" if r.get("method") == "poisson" and r.get("p") is not None
                 else f"{r['z']:.1f} 倍" if r.get("z") is not None else "規則")
            med = f"{r['median']:.4g}" if r.get("median") is not None else "—"
            val = f"{r['value']:.4g}" if r.get("value") is not None else "—"
            warn = "（warning）" if r.get("status") == "warning" else ""
            c.out.append(f"| {c.local(r['@timestamp'])}{warn} | `{r['series']}` | {(r.get('keys') or {}).get('segment', '')} "
                         f"| {val} | {med} | {z} | {_trust(r)} |")
        if len(found) > 10:
            c.out.append(f"| … | 共 {len(found)} 筆 | | | | | |")
        if any(r.get("confidence") == "low" for r in found):
            c.out += ["", "> 可信度 low：用的是不分時段、或借同類 API 的 baseline（不是自己的歷史），先確認是不是真的變了。"]
    else:
        c.out.append(f"這段時間 ⑤ 沒有記到這個{c.sc.entity}的異常（直接指定{c.sc.entity}查）。")
    c.out.append("")

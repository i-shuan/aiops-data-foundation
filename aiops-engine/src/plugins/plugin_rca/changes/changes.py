"""changes：出事前的變更（報告的一段；原本是 incident/rca_report.py 的 sec_changes）。
報告的骨架（Ctx、標題編號、共用的小工具）在 incident/rca_report.py；這個外掛只寫這一段。"""
from __future__ import annotations

from common.clock import parse_duration
from incident.rca_report import Ctx


def run(c: Ctx) -> None:
    """事故前的變更（PM、recipe 變更、部署、設定）：出事前 lookback 內、這幾個對象的變更紀錄，照時間列。rca_config 有 changes.query 才出。"""
    changes_config = c.rca_config.changes
    if changes_config is None or not changes_config.query:
        return
    lookback = changes_config.lookback
    c.heading(f"事故前 {lookback} 內的變更")
    try:
        c.changes = c.repo.logs.changes(c.ix.logs, c.suspects, c.start - parse_duration(lookback), c.end, changes_config.query)
    except Exception as ex:  # noqa: BLE001  變更紀錄查不到不擋報告
        c.out += [f"（事故前的變更查不到：{type(ex).__name__}）", ""]
        return
    c.findings["changes"] = [{"at": x["ts"], "service": x["service"], "message": x["message"].splitlines()[0][:200]} for x in c.changes[:10]]
    c.out += [f"- {c.local(str(x['ts']))}  {x['service']}  {x['message'].splitlines()[0][:150]}" for x in c.changes[:10]] or ["沒有變更紀錄。"]
    c.out.append("")

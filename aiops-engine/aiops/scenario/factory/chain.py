"""工廠的併件依據：前後站（docs/pdca/meta/2026-11-factory-engine.md）。
後站看到的異常（量測 CD 偏、良率掉）是前站做壞的。從履歷（lot-paths-*，scenario/factory/paths.py）查這批 lot 在出事機台之前
經過哪幾站、幾點在那站，事件整合（investigator/alerts.merge 的 links）把它併進前站那件、根因分析以前站跑。
IT 是從 trace 算「誰呼叫誰」（scenario/it/chain.py）；工廠沒有呼叫關係，但有 lot 的路徑，鍵是 lot id。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiops.core.clock import duration
from aiops.scenario.base import Chain


def _ms_to_dt(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def links_from_docs(anomalies: list[dict], docs: dict[str, dict]) -> dict[str, list[tuple[str, datetime]]]:
    """anomalies：這一輪的異常（keys 有 equipment、lot）；docs：{lot id: lot-paths 的文件}。
    回傳 {異常 _id: [(前站機台, lot 離開前站的時間), …]}，離出事機台最近的站排前面；同機台的站不算（那是自己）。
    出事機台在履歷裡出現好幾次（rework）時，取異常時間之前最近的那一次。"""
    out: dict[str, list[tuple[str, datetime]]] = {}
    for a in anomalies:
        keys = a.get("keys") or {}
        eq, lot = keys.get("equipment"), keys.get("lot")
        doc = docs.get(str(lot)) if lot is not None else None
        if not eq or not doc:
            continue
        steps = sorted([s for s in doc.get("spans") or [] if s.get("kind") == "Server"], key=lambda s: float(s["start_ms"]))
        t = datetime.fromisoformat(str(a["@timestamp"]).replace("Z", "+00:00"))
        mine = [i for i, s in enumerate(steps) if s.get("service") == eq and _ms_to_dt(float(s["start_ms"])) <= t + timedelta(minutes=5)]
        if not mine:
            continue
        pos = mine[-1]
        prev = [(s["service"], _ms_to_dt(float(s["start_ms"]) + float(s.get("dur_ms") or 0))) for s in reversed(steps[:pos]) if s.get("service") != eq]
        seen, uniq = set(), []
        for name, at in prev:
            if name not in seen:
                seen.add(name)
                uniq.append((name, at))
        if uniq:
            out[a["_id"]] = uniq
    return out


def lot_docs(repo, index: str, lots: set[str], start: datetime, end: datetime) -> dict[str, dict]:
    """這些 lot 的履歷文件（_id = lot id）。時間用履歷的 @timestamp（lot 開始的時間）往前框，lot 跑好幾天也框得到。"""
    return repo.traces.merged_docs(index, sorted(lots), start, end) if lots else {}


def lot_links(repo, index: str, anomalies: list[dict], now: datetime, lookback: timedelta = timedelta(days=3)) -> dict[str, list[tuple[str, datetime]]]:
    """事件整合用：這一輪帶 lot 的異常 → 前後站（見 links_from_docs）。履歷查不到的 lot 就沒有連結，不擋這一輪。"""
    lots = {str(a["keys"]["lot"]) for a in anomalies if (a.get("keys") or {}).get("lot") is not None and a["keys"].get("equipment")}
    if not lots:
        return {}
    return links_from_docs(anomalies, lot_docs(repo, index, lots, now - lookback, now + timedelta(minutes=5)))


class LotLinks:
    """前後站：這一輪帶 lot 的異常 → 從履歷（cfg.indices.traces）找這批 lot 之前經過哪幾站；cfg 的 alerts.paths_lookback 是往前找多久。"""

    def __call__(self, repo, cfg: dict, anomalies: list[dict], open_alerts: list, start: datetime, now: datetime) -> Chain:
        acfg = cfg.get("alerts", {})
        return Chain(links=lot_links(repo, cfg["indices"]["traces"], anomalies, now, duration(str(acfg.get("paths_lookback", "3d")))))

"""工廠版（半導體）在事件整合、嚴重度用到的兩件事（docs/pdca/meta/2026-11-factory-engine.md）：

  lot_links  前後站：後站看到的異常（量測 CD 偏、良率掉）是前站做壞的。從履歷（lot-paths-*，detector/path_merge.py）查這批 lot
             在出事機台之前經過哪幾站、幾點在那站，事件整合（alerts.merge 的 links）把它併進前站那件、根因分析以前站跑。
             IT 版是從 trace 算「誰呼叫誰」（cmd_alert.call_edges）；工廠沒有呼叫關係，但有 lot 的路徑，鍵是 lot id。
  change_at  recipe 變更當成「換版」：變更後 N 分鐐內的異常上限 warning（investigator/severity.py 的 deploy_grace）。
             IT 版看 service.version 第一次出現（cmd_alert.deploy_at）；工廠是變更紀錄一筆一筆的，直接拿最近一筆的時間。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.es import time_range


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


def lot_docs(es, index: str, lots: set[str], start: datetime, end: datetime) -> dict[str, dict]:
    """這些 lot 的履歷文件（_id = lot id）。時間用履歷的 @timestamp（lot 開始的時間）往前框，lot 跑好幾天也框得到。"""
    if not lots:
        return {}
    out, ids = {}, sorted(lots)
    for i in range(0, len(ids), 500):
        body = {"size": 500, "query": {"bool": {"filter": [time_range(start, end), {"terms": {"trace_id": ids[i:i + 500]}}]}}}
        for h in es.search(index, body).get("hits", {}).get("hits", []):
            out[h["_source"]["trace_id"]] = h["_source"]
    return out


def lot_links(es, index: str, anomalies: list[dict], now: datetime, lookback: timedelta = timedelta(days=3)) -> dict[str, list[tuple[str, datetime]]]:
    """事件整合用：這一輪帶 lot 的異常 → 前後站（見 links_from_docs）。履歷查不到的 lot 就沒有連結，不擋這一輪。"""
    lots = {str(a["keys"]["lot"]) for a in anomalies if (a.get("keys") or {}).get("lot") is not None and a["keys"].get("equipment")}
    if not lots:
        return {}
    return links_from_docs(anomalies, lot_docs(es, index, lots, now - lookback, now + timedelta(minutes=5)))


def change_at(es, index: str, entity_field: str, entity: str, since: datetime, now: datetime, time_field: str = "@timestamp",
              extra_filter: dict | None = None) -> datetime | None:
    """這台機台 since 之後最近一筆變更（recipe 變更、設定變更）的時間；沒有回 None。index 是變更紀錄（一筆一次變更）。"""
    f = [{"term": {entity_field: entity}}, {"range": {time_field: {"gte": since.isoformat(), "lt": now.isoformat()}}}]
    if extra_filter:
        f.append(extra_filter)
    body = {"size": 1, "_source": [time_field], "sort": [{time_field: "desc"}], "query": {"bool": {"filter": f}}}
    hits = es.search(index, body).get("hits", {}).get("hits", [])
    if not hits:
        return None
    v = hits[0]["_source"].get(time_field)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None

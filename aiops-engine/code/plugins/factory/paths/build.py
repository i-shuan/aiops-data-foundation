"""⑦ 履歷合成器（工廠 plugin）：MES 進出站＋S6F11 機台事件 → 一個 lot 一筆（lot-paths-<group>-<日期>），文件格式跟 traces-merged 一樣
（detector/trace_merge.build），所以 M4 trace 分段、根因分析（慢在哪一站、壞 lot 的共同點）、事件整合的前後站都照舊，只是：

  trace id   = lot id
  根 span    = 這個 lot（service "lot"、name 是產品）
  入口 span  = 一站（Server）：service 是機台、name 是 recipe、耗時是進站到出站；result 是 hold／scrap 的算錯
  子 span    = 站裡面的機台事件（S6F11：chamber、recipe step、ProcessStart→ProcessEnd），掛在那一站底下（Internal）

設定（configs/factory/paths.yaml）：

  mes:
    index: mes-history-*
    fields: {lot: lot_id, step: step_no, station: station, equipment: equipment_id, chamber: chamber_id, recipe: recipe_id,
             recipe_version: recipe_version, product: product_id, start: track_in, end: track_out, result: result}
    bad_results: [hold, scrap, rework, fail]
    attrs: [operator, carrier_id]          # 選填：其他要帶進 span attrs 的欄位（根因分析的 commonality 可以拿來比）
  events:                                  # 選填（沒有 S6F11 就只有站）
    index: eqp-events-*
    fields: {lot: lot_id, equipment: equipment_id, chamber: chamber_id, step: recipe_step, start: start_time, end: end_time, event: ceid}
    attrs: [ceid, wafer_count]
  group: fab1                              # index 名的 group；或 group_field: area（從 MES 列讀）
  dept_field: dept                         # 選填：MES 列裡的部門欄位（Grafana 分送用）

欄位名全是假設（沒有 MES、EAP 的樣本），拿到樣本換 fields 右邊就好。這裡只做合成（單元測試用）；讀寫 ES 在 cmd_paths.py。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from core.es_client import get_field
from detector.trace_merge import build

PATHS_PREFIX = "lot-paths"
ROOT_SERVICE = "lot"
DEFAULT_BAD = ("hold", "scrap", "rework", "fail", "abort")
# 跟 traces-merged 同一份 mapping（elasticsearch/kibana-dev-tools.txt 第 2 段）；工廠的 ES 沒有 es-setup，engine 自己放
PATHS_TEMPLATE = {
    "index_patterns": [f"{PATHS_PREFIX}-*"], "priority": 200,
    "template": {"settings": {"index.number_of_shards": 1, "index.mapping.nested_objects.limit": 20000},
                 "mappings": {"dynamic": False, "properties": {
                     "@timestamp": {"type": "date"}, "merged_at": {"type": "date"}, "trace_id": {"type": "keyword"},
                     "group": {"type": "keyword"}, "dept": {"type": "keyword"},
                     "root": {"properties": {"service": {"type": "keyword"}, "name": {"type": "keyword"}, "kind": {"type": "keyword"}, "error": {"type": "boolean"}}},
                     "duration_ms": {"type": "double"}, "error": {"type": "boolean"}, "services": {"type": "keyword"}, "span_count": {"type": "integer"},
                     "entries": {"type": "nested", "properties": {
                         "span_id": {"type": "keyword"}, "service": {"type": "keyword"}, "name": {"type": "keyword"}, "kind": {"type": "keyword"},
                         "start_ms": {"type": "double"}, "duration_ms": {"type": "double"}, "error": {"type": "boolean"}}},
                     "spans": {"type": "nested", "properties": {
                         "span_id": {"type": "keyword"}, "parent": {"type": "keyword"}, "service": {"type": "keyword"}, "name": {"type": "keyword"},
                         "kind": {"type": "keyword"}, "target": {"type": "keyword"}, "start_ms": {"type": "double"}, "dur_ms": {"type": "double"},
                         "error": {"type": "boolean"}, "exception": {"type": "keyword"}, "links": {"type": "keyword"},
                         "attrs": {"type": "flattened"}, "resource": {"type": "flattened"}}}}}},
    "_meta": {"description": "一個 lot 一筆（detector path-merge 從 MES 進出站＋S6F11 合成；_id = lot id）。格式跟 traces-merged 一樣：spans 每一站、站裡的機台事件；entries 是站（依機台／recipe 挑 lot 用）。"},
}


def _ms(x) -> float | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.timestamp() * 1000.0


def _s(x):
    return None if x is None or (isinstance(x, float) and pd.isna(x)) else str(x)


def _attrs(row: dict, names) -> dict:
    return {n: v for n in (names or []) if (v := get_field(row, n)) is not None and not (isinstance(v, float) and pd.isna(v))}


def build_lot(lot: str, steps: list[dict], events: list[dict], cfg: dict, now: datetime | None = None) -> list[dict]:
    """一個 lot 的 MES 站（steps）與機台事件（events）→ span 清單（sources/traces 的格式）。
    站依進站時間排；還沒出站的站用 now 當結束。機台事件掛到同機台、時間落在那一站裡的站底下；對不到站的事件丟掉。"""
    now = now or datetime.now(timezone.utc)
    mf, ef = cfg["mes"]["fields"], (cfg.get("events") or {}).get("fields") or {}
    bad = {str(b).lower() for b in (cfg["mes"].get("bad_results") or DEFAULT_BAD)}
    group = cfg.get("group") or "unassigned"
    rows = []
    for r in steps:
        start = _ms(get_field(r, mf["start"]))
        if start is None:
            continue
        end = _ms(get_field(r, mf["end"])) if mf.get("end") else None
        rows.append((start, end if end is not None else now.timestamp() * 1000.0, end is None, r))
    if not rows:
        return []
    rows.sort(key=lambda x: x[0])
    first = rows[0][3]
    g = _s(get_field(first, cfg["group_field"])) if cfg.get("group_field") else None
    dept = _s(get_field(first, cfg["dept_field"])) if cfg.get("dept_field") else None
    product = _s(get_field(first, mf["product"])) if mf.get("product") else None
    t0, t1 = rows[0][0], max(e for _, e, _, _ in rows)
    root_res = {"service.name": ROOT_SERVICE, "group": g or group}
    if dept:
        root_res["dept"] = dept
    spans = [{"trace_id": lot, "span_id": lot, "parent": None, "service": ROOT_SERVICE, "name": product or "lot", "kind": "Internal",
              "target": None, "start_ms": t0, "dur_ms": max(0.0, t1 - t0), "error": False, "exception": None, "links": [],
              "attrs": {"lot.id": lot, **({"product": product} if product else {})}, "resource": root_res}]
    step_spans = []
    for i, (start, end, open_, r) in enumerate(rows):
        eq = _s(get_field(r, mf["equipment"])) or "?"
        recipe = _s(get_field(r, mf["recipe"])) if mf.get("recipe") else None
        station = _s(get_field(r, mf["station"])) if mf.get("station") else None
        step_no = _s(get_field(r, mf["step"])) if mf.get("step") else None
        result = _s(get_field(r, mf["result"])) if mf.get("result") else None
        chamber = _s(get_field(r, mf["chamber"])) if mf.get("chamber") else None
        rv = _s(get_field(r, mf["recipe_version"])) if mf.get("recipe_version") else None
        attrs = {"lot.id": lot, **{k: v for k, v in (("recipe.id", recipe), ("recipe.version", rv), ("step", step_no), ("station", station),
                                                     ("chamber", chamber), ("result", result), ("product", product)) if v is not None},
                 **_attrs(r, cfg["mes"].get("attrs"))}
        if open_:
            attrs["in_progress"] = True
        res = {"service.name": eq, "group": g or group, **({"dept": dept} if dept else {}), **({"chamber": chamber} if chamber else {}),
               **({"recipe.version": rv} if rv else {})}
        sid = f"{lot}/{step_no if step_no is not None else i + 1}"
        step_spans.append({"trace_id": lot, "span_id": sid, "parent": lot, "service": eq, "name": recipe or station or f"step {i + 1}",
                           "kind": "Server", "target": None, "start_ms": start, "dur_ms": max(0.0, end - start),
                           "error": bool(result and result.lower() in bad), "exception": None, "links": [], "attrs": attrs, "resource": res})
    spans += step_spans
    # 機台事件（S6F11）掛到同機台、時間落在那一站裡的站
    for k, e in enumerate(events or []):
        es_ = _ms(get_field(e, ef["start"])) if ef.get("start") else None
        if es_ is None:
            continue
        ee = _ms(get_field(e, ef["end"])) if ef.get("end") else None
        eq = _s(get_field(e, ef["equipment"])) if ef.get("equipment") else None
        host = None
        for s in step_spans:
            if (eq is None or s["service"] == eq) and s["start_ms"] - 60_000 <= es_ <= s["start_ms"] + s["dur_ms"] + 60_000:
                host = s
                break
        if host is None:
            continue
        chamber = _s(get_field(e, ef["chamber"])) if ef.get("chamber") else None
        rstep = _s(get_field(e, ef["step"])) if ef.get("step") else None
        name = " ".join(x for x in (chamber, rstep) if x) or (_s(get_field(e, ef["event"])) if ef.get("event") else None) or "event"
        attrs = {"lot.id": lot, **{k2: v for k2, v in (("chamber", chamber), ("recipe.step", rstep)) if v is not None}, **_attrs(e, (cfg.get("events") or {}).get("attrs"))}
        if ef.get("event") and (ev := _s(get_field(e, ef["event"]))):
            attrs["event"] = ev
        spans.append({"trace_id": lot, "span_id": f"{host['span_id']}/e{k + 1}", "parent": host["span_id"], "service": host["service"],
                      "name": name, "kind": "Internal", "target": None, "start_ms": es_,
                      "dur_ms": max(0.0, (ee if ee is not None else es_) - es_), "error": False, "exception": None, "links": [],
                      "attrs": attrs, "resource": {**host["resource"], **({"chamber": chamber} if chamber else {})}})
    return spans


def group_rows(rows: list[dict], lot_field: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        lot = _s(get_field(r, lot_field))
        if lot:
            out.setdefault(lot, []).append(r)
    return out


def build_docs(mes_rows: list[dict], event_rows: list[dict], cfg: dict, now: datetime | None = None) -> dict[str, list[tuple[str, dict]]]:
    """MES 列、機台事件列 → {index: [(lot id, 文件), …]}。"""
    now = now or datetime.now(timezone.utc)
    lots = group_rows(mes_rows, cfg["mes"]["fields"]["lot"])
    ev_lot = (cfg.get("events") or {}).get("fields", {}).get("lot")
    evs = group_rows(event_rows, ev_lot) if ev_lot and event_rows else {}
    docs: dict[str, list[tuple[str, dict]]] = {}
    for lot, steps in lots.items():
        spans = build_lot(lot, steps, evs.get(lot, []), cfg, now)
        if not spans:
            continue
        index, doc = build(spans, now, prefix=PATHS_PREFIX)
        docs.setdefault(index, []).append((lot, doc))
    return docs

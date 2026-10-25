"""⑥ 事件整合（最小版）：同一個服務、時間接在一起的異常合成一件（docs/pdca/2026-10-alert-exit.md）。

合成一件看「服務」：同一個 dept 的兩個不相關服務出事是兩件。送給誰看 dept，由 Grafana 依 aiops-alerts 的 dept 分送。

哪些異常會開一件：
  open    ：規則 severity critical、baseline／trace baseline 的 status anomaly（已經過「連續才算」）、資料品質
  weak    ：規則 warning、跟兄弟比、trace 的規則（N+1、吞掉的例外…）：自己不開；同一個服務 close_after 內有 2 筆以上才開
  attach  ：baseline status warning（只跳一次）：只併進已經開著的那件，不會開新的
同一個服務已經有開著的那件、而且離它最後一筆異常不到 close_after：併進去；超過 close_after 沒有新的異常：resolved。

這裡只做計算（單元測試用）；讀寫 ES、跑根因分析在 cmd_alert.py。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta

SVC_KEYS = ("resource.attributes.service.name", "service", "entry.service", "service.name", "caller")
NS_KEYS = ("resource.attributes.k8s.namespace.name", "k8s.namespace.name", "namespace", "kubernetes.namespace")
NODE_KEYS = ("resource.attributes.k8s.node.name", "k8s.node.name", "node")
SEVERITY = {"warning": 1, "critical": 2}
MAX_IDS = 500          # 一件最多記幾筆異常 id（很長的事故不要讓文件無限長）


def _ts(s) -> datetime:
    return s if isinstance(s, datetime) else datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def entity_of(a: dict) -> tuple[str, str]:
    """(類別, 名稱)：這筆異常歸到哪一件。服務優先；沒有服務的用 namespace、再用節點；資料品質另外一類（送平台）。"""
    keys = a.get("keys") or {}
    cat = "data_quality" if a.get("level") == "quality" else "service"
    for kind, fields in (("service", SVC_KEYS), ("namespace", NS_KEYS), ("node", NODE_KEYS)):
        for f in fields:
            if keys.get(f):
                return (cat if kind == "service" or cat == "data_quality" else kind), str(keys[f])
    if cat == "data_quality" and keys:
        return cat, "、".join(str(v) for v in keys.values())
    return cat if cat == "data_quality" else "other", a.get("series", "?")


def kind_of(a: dict) -> str:
    """open／weak／attach。"""
    level = a.get("level")
    if level == "quality":
        return "open"
    if level in ("rule", "peer"):
        return "open" if a.get("severity") == "critical" else "weak"
    return "attach" if a.get("status") == "warning" else "open"   # baseline、trace baseline


def route_of(keys: dict) -> str | None:
    """哪支 API（跟根因分析 _route_of 同一套寫法）。"""
    if keys.get("entry.name"):
        return keys["entry.name"]
    for m, r in (("attributes.http.request.method", "attributes.http.route"), ("method", "route")):
        if keys.get(m) and keys.get(r):
            return f"{keys[m]} {keys[r]}"
    return None


def alert_id(cat: str, name: str, started: datetime) -> str:
    return f"a-{started:%Y%m%d-%H%M}-{re.sub(r'[^0-9A-Za-z_.-]+', '_', name)}" + ("-dq" if cat == "data_quality" else "")


def _new(cat: str, name: str, first: dict) -> dict:
    t = _ts(first["@timestamp"])
    return {"alert_id": alert_id(cat, name, t), "category": cat, "service": name, "status": "open",
            "started_at": t, "updated_at": t, "resolved_at": None, "anomaly_ids": [], "series": [], "routes": [],
            "severity": "warning", "_anoms": [], "_new": True}


def _attach(al: dict, a: dict) -> None:
    t = _ts(a["@timestamp"])
    al["updated_at"] = max(_ts(al["updated_at"]), t)
    al["started_at"] = min(_ts(al["started_at"]), t)
    if len(al["anomaly_ids"]) < MAX_IDS:
        al["anomaly_ids"].append(a["_id"])
    if a.get("series") and a["series"] not in al["series"]:
        al["series"].append(a["series"])
    r = route_of(a.get("keys") or {})
    if r and r not in al["routes"]:
        al["routes"].append(r)
    sev = a.get("severity") or "warning"
    if SEVERITY.get(sev, 1) > SEVERITY.get(al["severity"], 1):
        al["severity"], al["_upgraded"] = sev, not al.get("_new")
    al.setdefault("_anoms", []).append(a)


def merge(open_alerts: list[dict], anomalies: list[dict], now: datetime, close_after: timedelta,
          done_ids: set | frozenset = frozenset()) -> list[dict]:
    """open_alerts：aiops-alerts 裡 status open 的；anomalies：最近的異常（含 _id）；
    done_ids：最近已經結束的件裡的異常 id（不要再被開一次，重跑才不會重複）。
    回傳這次要寫回的件：新開的（_new）、有更新的、這次結束的（resolved），以及其他還開著的（更新心跳）。"""
    by_ent = {(al["category"], al["service"]): {**al, "anomaly_ids": list(al.get("anomaly_ids") or []),
                                                 "series": list(al.get("series") or []), "routes": list(al.get("routes") or [])}
              for al in open_alerts}
    seen = {i for al in by_ent.values() for i in al["anomaly_ids"]} | set(done_ids)
    pending: dict[tuple, list[dict]] = {}
    out: dict[str, dict] = {al["alert_id"]: al for al in by_ent.values()}
    for a in sorted(anomalies, key=lambda a: _ts(a["@timestamp"])):
        if a["_id"] in seen:
            continue
        seen.add(a["_id"])
        ent, t, k = entity_of(a), _ts(a["@timestamp"]), kind_of(a)
        al = by_ent.get(ent)
        if al and t - _ts(al["updated_at"]) > close_after:      # 隔太久：舊的那件結束，這筆當新的看
            al["status"], al["resolved_at"] = "resolved", _ts(al["updated_at"]) + close_after
            del by_ent[ent]
            al = None
        if al and t < _ts(al["started_at"]) - close_after:   # 比這件開始還早很多：屬於更早的那件，不併
            continue
        if al:
            _attach(al, a)
            continue
        if k == "attach":
            continue
        if k == "weak":
            pend = [p for p in pending.get(ent, []) if t - _ts(p["@timestamp"]) <= close_after] + [a]
            pending[ent] = pend
            if len(pend) < 2:
                continue
            first, rest = pend[0], pend
        else:
            first, rest = a, [*pending.pop(ent, []), a]
        al = _new(*ent, first)
        for p in sorted(rest, key=lambda p: _ts(p["@timestamp"])):
            _attach(al, p)
        pending.pop(ent, None)
        by_ent[ent] = al
        out[al["alert_id"]] = al
    for al in by_ent.values():
        if now - _ts(al["updated_at"]) > close_after:
            al["status"], al["resolved_at"] = "resolved", now
    return list(out.values())


def owner(al: dict) -> tuple[str | None, str]:
    """這件的 dept、group：取它的異常裡最多的（異常寫入時已補上，detector/owner.py）。資料品質一律送平台。"""
    if al["category"] == "data_quality":
        return None, "unassigned"
    anoms = al.get("_anoms") or []
    dept = Counter(a["dept"] for a in anoms if a.get("dept")).most_common(1)
    group = Counter(a["group"] for a in anoms if a.get("group")).most_common(1)
    return (dept[0][0] if dept else al.get("dept")), (group[0][0] if group else al.get("group") or "unassigned")


def headline(al: dict) -> str:
    """一行標題：嚴重度、服務、最嚴重的那筆、共幾項。"""
    anoms = al.get("_anoms") or []
    top = max(anoms, key=lambda a: (SEVERITY.get(a.get("severity") or "warning", 1), abs(a.get("z") or 0)), default=None)
    what = ""
    if top:
        msg = top.get("message")
        if msg:
            what = msg.split("：", 1)[0] if "：" in msg else msg
        else:
            med = f"（平常 {top['median']:.4g}）" if top.get("median") is not None else ""
            what = f"{top.get('series')} {top.get('value', 0):.4g}{med}"
    n = len(al["anomaly_ids"])
    return f"[{al['severity']}] {al['service']}：{what}" + (f" 等 {n} 項異常" if n > 1 else "")


def conclusion(report: str) -> list[str]:
    """根因報告「結論」那一段的條列（investigate 的輸出）。"""
    m = re.search(r"^## 結論[^\n]*\n(.*)", report or "", re.S | re.M)
    return [l[2:].strip() for l in (m.group(1).splitlines() if m else []) if l.startswith("- ")]


def summary(al: dict, report: str | None, tz) -> str:
    start = _ts(al["started_at"]).astimezone(tz)
    mins = int((_ts(al["updated_at"]) - _ts(al["started_at"])).total_seconds() // 60)
    lines = [headline(al), f"從 {start:%m-%d %H:%M} 開始，持續 {mins} 分鐘；異常：{'、'.join(al['series'][:6])}"]
    concl = conclusion(report or "")
    if concl:
        lines.append("根因分析：" + "；".join(re.sub(r"\*\*", "", c) for c in concl[:3]))
    return "\n".join(lines)

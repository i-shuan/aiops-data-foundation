"""⑥ 事件整合（最小版）：同一個服務、時間接在一起的異常合成一件（docs/pdca/phase3-locate/2026-10-alert-exit.md）。

合成一件看「服務」：同一個 dept 的兩個不相關服務出事是兩件。送給誰看 dept，由 Grafana 依 aiops-alerts 的 dept 分送。

哪些異常會開一件：
  open    ：規則 severity critical、baseline／trace baseline 的 status anomaly（已經過「連續才算」）、資料品質
  weak    ：規則 warning、跟兄弟比、trace 的規則（N+1、吞掉的例外…）：自己不開；同一個服務 close_after 內有 2 筆以上才開
  attach  ：baseline status warning（只跳一次）：只併進已經開著的那件，不會開新的
同一個服務已經有開著的那件、而且離它最後一筆異常不到 close_after：併進去；超過 close_after 沒有新的異常：resolved。

跨服務（一層父子關係，docs/pdca/phase3-locate/2026-10-cross-service-aggregation.md）：calls 是這段時間 trace 算出來的
「上游 → 下游」（plugins/observability/chain.py，用根因分析同一套 topology）。下游出事連帶上游異常時只開一件、留下游那件：
  - 上游服務的異常、自己沒有開著的件、它直接呼叫的下游有開著的件、時間重疊（離下游那件開始不到 close_after）→ 併進下游那件，
    下游那件的 upstream_services 記這個上游；之後同一個上游的異常照同服務的規則併（離最後一筆不到 close_after）。
  - 下游開新的一件時，直接呼叫它的上游若已經有一件、開始時間差不到 close_after → 上游那件整件併進下游：這一輪才開的直接消失；
    上一輪已寫進 ES 的標 resolved、merged_into 指到下游那件（Grafana 看到上游那件恢復、下游那件開）。
  - 只看直接呼叫（一層）：A→B→C 時 B 併進 C，A 不併進 C；已經帶 upstream_services 的件不會再被併進別件。
  - 沒有呼叫關係、或時間不重疊：照舊各開一件。

這裡只做計算（單元測試用）；讀寫 ES、跑根因分析在 cmd_alert.py。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta

from core import plugin

# 異常的 keys 裡哪個欄位是「對象」（一件的主體：服務／機台）由 plugin 決定（core/plugin.py 的 entity_keys）；namespace、節點是共通的 fallback
NS_KEYS = ("resource.attributes.k8s.namespace.name", "k8s.namespace.name", "namespace", "kubernetes.namespace")
NODE_KEYS = ("resource.attributes.k8s.node.name", "k8s.node.name", "node")
SEVERITY = {"warning": 1, "high": 2, "critical": 3}   # 一件最後的嚴重度由 severity.grade 算（訊號廣度＋持續＋換版上限）；這裡只是異常自己帶的
MAX_IDS = 500          # 一件最多記幾筆異常 id（很長的事故不要讓文件無限長）


def _ts(s) -> datetime:
    return s if isinstance(s, datetime) else datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def entity_of(a: dict) -> tuple[str, str]:
    """(類別, 名稱)：這筆異常歸到哪一件。服務優先；沒有服務的用 namespace、再用節點；資料品質另外一類（送平台）。"""
    keys = a.get("keys") or {}
    cat = "data_quality" if a.get("level") == "quality" else "service"
    for kind, fields in (("service", plugin.current().entity_keys), ("namespace", NS_KEYS), ("node", NODE_KEYS)):
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
    """「哪支 API」：IT 是 trace 入口的名稱或 method＋route，工廠是 recipe（plugin 決定；根因分析挑樣本也用它）。"""
    return plugin.current().route_of(keys or {})


def alert_id(cat: str, name: str, started: datetime) -> str:
    return f"a-{started:%Y%m%d-%H%M}-{re.sub(r'[^0-9A-Za-z_.-]+', '_', name)}" + ("-dq" if cat == "data_quality" else "")


def _new(cat: str, name: str, first: dict) -> dict:
    t = _ts(first["@timestamp"])
    return {"alert_id": alert_id(cat, name, t), "category": cat, "service": name, "status": "open",
            "started_at": t, "updated_at": t, "resolved_at": None, "anomaly_ids": [], "series": [], "routes": [], "lots": [],
            "upstream_services": [], "severity": "warning", "_anoms": [], "_new": True}


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
    lot = (a.get("keys") or {}).get("lot")
    if lot is not None and str(lot) not in al.setdefault("lots", []) and len(al["lots"]) < MAX_IDS:
        al["lots"].append(str(lot))
    sev = a.get("severity") or "warning"
    if SEVERITY.get(sev, 1) > SEVERITY.get(al["severity"], 1):
        al["severity"], al["_upgraded"] = sev, not al.get("_new")
    al.setdefault("_anoms", []).append(a)


def _join(al: dict, upstream: str) -> None:
    """記下「這件連帶了哪個上游服務」。"""
    if upstream not in al["upstream_services"]:
        al["upstream_services"].append(upstream)


def _fold(up: dict, down: dict, now: datetime) -> None:
    """上游那件（up）整件併進下游那件（down）：異常 id、序列、API、嚴重度、這一輪的異常都搬過去。
    up 是上一輪已寫進 ES 的：標 resolved、merged_into（cmd_alert 照寫回去，但不再把它的異常標成它、不寫事故）。"""
    for i in up["anomaly_ids"]:
        if i not in down["anomaly_ids"] and len(down["anomaly_ids"]) < MAX_IDS:
            down["anomaly_ids"].append(i)
    down["series"] += [s for s in up["series"] if s not in down["series"]]
    down["routes"] += [r for r in up["routes"] if r not in down["routes"]]
    down.setdefault("lots", [])
    down["lots"] += [l for l in up.get("lots") or [] if l not in down["lots"]][:max(0, MAX_IDS - len(down["lots"]))]
    if SEVERITY.get(up["severity"], 1) > SEVERITY.get(down["severity"], 1):
        down["severity"] = up["severity"]
    down["started_at"] = min(_ts(down["started_at"]), _ts(up["started_at"]))
    down["updated_at"] = max(_ts(down["updated_at"]), _ts(up["updated_at"]))
    down["_anoms"] += up.get("_anoms") or []
    _join(down, up["service"])
    if not up.get("_new"):
        up["status"], up["resolved_at"], up["merged_into"], up["_anoms"] = "resolved", now, down["alert_id"], []


def merge(open_alerts: list[dict], anomalies: list[dict], now: datetime, close_after: timedelta,
          done_ids: set | frozenset = frozenset(), calls: set | frozenset = frozenset(),
          links: dict[str, list[tuple[str, datetime]]] | None = None) -> list[dict]:
    """open_alerts：aiops-alerts 裡 status open 的；anomalies：最近的異常（含 _id）；
    done_ids：最近已經結束的件裡的異常 id（不要再被開一次，重跑才不會重複）；
    calls：{(上游服務, 下游服務)}，這段時間 trace 裡誰呼叫誰（沒有就不併跨服務）；
    links：工廠的前後站（plugins/factory/chain.lot_links）：{異常 _id: [(前站機台, 這批 lot 在前站的時間), …]}。
      後站（量測站）看到的異常是前站做壞的：前站有開著的件、而且 lot 在前站的時間落在那件的範圍內（前後各 close_after）→
      併進前站那件、記 upstream_services（跟 IT 的「上游併進下游」同一個欄位：被併進去的那一方）；自己已經有件就照同對象的規則。
    回傳這次要寫回的件：新開的（_new）、有更新的、這次結束的（resolved，含被併進下游的 merged_into），以及其他還開著的（更新心跳）。"""
    links = links or {}
    by_ent = {(al["category"], al["service"]): {**al, "anomaly_ids": list(al.get("anomaly_ids") or []),
                                                 "series": list(al.get("series") or []), "routes": list(al.get("routes") or []),
                                                 "lots": list(al.get("lots") or []),
                                                 "upstream_services": list(al.get("upstream_services") or [])}
              for al in open_alerts}
    down_of: dict[str, set[str]] = {}
    up_of: dict[str, set[str]] = {}
    for u, d in calls:
        if u != d:
            down_of.setdefault(u, set()).add(d)
            up_of.setdefault(d, set()).add(u)
    seen = {i for al in by_ent.values() for i in al["anomaly_ids"]} | set(done_ids)
    pending: dict[tuple, list[dict]] = {}
    out: dict[str, dict] = {al["alert_id"]: al for al in by_ent.values()}

    def downstream_alert(svc: str, t: datetime) -> dict | None:
        """svc 直接呼叫的下游裡，有開著的件、時間重疊的那件（有好幾件取開始時間最近的）。"""
        cands = []
        for d in down_of.get(svc, ()):
            al = by_ent.get(("service", d))
            if not al:
                continue
            joined = svc in al["upstream_services"]
            if joined and t - _ts(al["updated_at"]) <= close_after and t >= _ts(al["started_at"]) - close_after:
                cands.append((timedelta(0), al))                      # 已經連帶過：照同服務的規則
            elif not joined and abs(t - _ts(al["started_at"])) <= close_after:
                cands.append((abs(t - _ts(al["started_at"])), al))    # 第一次：離下游那件開始不到 close_after
        return min(cands, key=lambda c: c[0])[1] if cands else None

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
        if ent[0] == "service":
            al = downstream_alert(ent[1], t)
            if al is None and a["_id"] in links:
                # 工廠：這批 lot 經過的前站有開著的件 → 併進前站那件。先找「那件已經有這批 lot」的前站（lot 在前站就異常了，最有力）；
                # 沒有就取離它最近、而且 lot 在前站的時間落在那件範圖內（前後各 close_after）的前站
                lot = str((a.get("keys") or {}).get("lot"))
                cands = [(name, at, by_ent.get(("service", name))) for name, at in links[a["_id"]] if name != ent[1]]
                cands = [(n_, at, c) for n_, at, c in cands if c]
                al = next((c for _, _, c in cands if lot in (c.get("lots") or [])), None)
                if al is None:
                    al = next((c for _, at, c in cands if _ts(c["started_at"]) - close_after <= _ts(at) <= _ts(c["updated_at"]) + close_after), None)
            if al:                                             # 上游的異常：併進下游那件
                _attach(al, a)
                _join(al, ent[1])
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
        if ent[0] == "service":                                # 直接呼叫它的上游已經有一件、同時開始：整件併進來
            for u in sorted(up_of.get(ent[1], ())):
                up = by_ent.get(("service", u))
                if up and not up["upstream_services"] and abs(_ts(up["started_at"]) - _ts(al["started_at"])) <= close_after:
                    _fold(up, al, now)
                    del by_ent[("service", u)]
                    if up.get("_new"):
                        del out[up["alert_id"]]
        by_ent[ent] = al
        out[al["alert_id"]] = al
    for al in by_ent.values():
        if now - _ts(al["updated_at"]) > close_after:
            al["status"], al["resolved_at"] = "resolved", now
    return list(out.values())


def _own(al: dict) -> list[dict]:
    """這一輪的異常裡，屬於這件自己服務的（連帶進來的上游不算）；一筆都沒有就全部。"""
    anoms = al.get("_anoms") or []
    own = [a for a in anoms if entity_of(a) == (al["category"], al["service"])]
    return own or anoms


def rca_route(al: dict) -> str | None:
    """根因分析只查一支 API 的條件：這件自己服務的異常只有一支 API。連帶進來的上游的 API 不算（下游沒有那支，查了會是空的）。"""
    if not al.get("upstream_services"):
        return al["routes"][0] if len(al["routes"]) == 1 else None
    own = {route_of(a.get("keys") or {}) for a in _own(al)} - {None}
    return next(iter(own)) if len(own) == 1 else None


def owner(al: dict) -> tuple[str | None, str]:
    """這件的 dept、group：取它的異常裡最多的（異常寫入時已補上，detector/owner.py）；連帶的上游不算（根因在下游，送下游的部門）。
    資料品質一律送平台。"""
    if al["category"] == "data_quality":
        return None, "unassigned"
    anoms = _own(al)
    dept = Counter(a["dept"] for a in anoms if a.get("dept")).most_common(1)
    group = Counter(a["group"] for a in anoms if a.get("group")).most_common(1)
    return (dept[0][0] if dept else al.get("dept")), (group[0][0] if group else al.get("group") or "unassigned")


def dependencies(findings: dict | None) -> list[str]:
    """這件卡在哪些外部依賴：根因結果裡慢的那幾段、錯誤源頭那幾段「→」後面的對象（oracle、sms.example.com…）。"""
    f = findings or {}
    segs = [d["segment"] for d in f.get("segments") or [] if (d.get("delta_ms") or 0) > 0][:3]
    segs += [d["segment"] for d in f.get("error_origins") or []][:3]
    out: list[str] = []
    for s in segs:
        if " → " in s:
            t = s.rsplit(" → ", 1)[1].strip()
            if t and t not in out:
                out.append(t)
    return out


def link_shared(alerts: list[dict]) -> set[str]:
    """開著的、不同服務的件卡在同一個外部依賴（同一個 Oracle）→ 互相記 related_alerts、shared_dependency。
    不合併：是不同服務各自的件（部門、處置不同），只提示很可能同源。對象是開著的件的服務時不算（那是上下游，chain 處理）。
    回傳 related_alerts 有變的 alert_id（摘要要重寫）。"""
    opens = [al for al in alerts if al.get("status") == "open" and al.get("category") == "service" and not al.get("merged_into")]
    svcs = {al["service"] for al in opens}
    for al in opens:
        al["dependencies"] = [d for d in dependencies(al.get("findings")) if d not in svcs]
    changed = set()
    for al in opens:
        rel, shared = [], []
        for other in opens:
            if other is al or other["service"] == al["service"]:
                continue
            common = [d for d in al["dependencies"] if d in other["dependencies"]]
            if common:
                rel.append(other["alert_id"])
                shared += [c for c in common if c not in shared]
        rel = sorted(rel)
        if rel != sorted(al.get("related_alerts") or []):
            changed.add(al["alert_id"])
        al["related_alerts"], al["shared_dependency"] = rel, shared
    return changed


def headline(al: dict, count: bool = True) -> str:
    """一行標題：嚴重度、服務、最嚴重的那筆（自己服務的優先）、共幾項；連帶了上游就加在最後。"""
    top = max(_own(al), key=lambda a: (SEVERITY.get(a.get("severity") or "warning", 1), abs(a.get("z") or 0)), default=None)
    what = ""
    if top:
        msg = top.get("message")
        if msg:
            what = msg.split("：", 1)[0] if "：" in msg else msg
        else:
            med = f"（平常 {top['median']:.4g}）" if top.get("median") is not None else ""
            what = f"{top.get('series')} {top.get('value', 0):.4g}{med}"
    n = len(al["anomaly_ids"])
    up = al.get("upstream_services") or []
    return (f"[{al['severity']}] {al['service']}：{what}" + (f" 等 {n} 項異常" if count and n > 1 else "")
            + (f"；連帶上游 {'、'.join(up)}" if up else ""))


def conclusion(report: str) -> list[str]:
    """根因報告「結論」那一段的條列（investigate 的輸出）。"""
    m = re.search(r"^## 結論[^\n]*\n(.*)", report or "", re.S | re.M)
    return [l[2:].strip() for l in (m.group(1).splitlines() if m else []) if l.startswith("- ")]


def summary(al: dict, report: str | None, tz, similar: list[dict] | None = None) -> str:
    """通知用的摘要。Grafana 把它當 label（alert-rules.yaml 的 terms summary）：內容一變，Grafana 就當成另一則告警
    （舊的 resolved、新的 firing），所以不放每分鐘都會變的東西——「持續 N 分鐘」、「等 N 項異常」都不放
    （Grafana 自己會顯示從什麼時候開始；筆數在 title 和 anomaly_count）。只在根因結論、相似事故、連帶的上游加進來時才變。"""
    start = _ts(al["started_at"]).astimezone(tz)
    lines = [headline(al, count=False), f"從 {start:%m-%d %H:%M} 開始；異常：{'、'.join(al['series'][:6])}"]
    concl = conclusion(report or "")
    if concl:
        lines.append("根因分析：" + "；".join(re.sub(r"\*\*", "", c) for c in concl[:3]))
    if al.get("shared_dependency") and al.get("related_alerts"):
        lines.append(f"可能同源：{'、'.join(al['related_alerts'][:3])} 也卡在 {'、'.join(al['shared_dependency'][:3])}")
    if similar:
        # case 只做分類：通知裡只說像哪一類、為什麼像，不給當時的處置（docs/pdca/meta/2026-11-case-classify.md）
        from .similar import classify, describe_class
        lines.append(f"分類：{describe_class(classify(similar))}；最像 {(similar[0].get('start') or '')[:10]}（{'；'.join(similar[0]['why'][:2])}）")
    return "\n".join(lines)

"""⑥ 事件整合（最小版）：同一個服務、時間接在一起的異常合成一件（docs/pdca/phase3-locate/2026-10-alert-exit.md）。

合成一件看「服務」：同一個 dept 的兩個不相關服務出事是兩件。送給誰看 dept，由 Grafana 依 aiops-alerts 的 dept 分送。

哪些異常會開一件：
  open    ：規則 severity critical、baseline／trace baseline 的 status anomaly（已經過「連續才算」）、資料品質
  weak    ：規則 warning、跟兄弟比、trace 的規則（N+1、吞掉的例外…）：自己不開；同一個服務 close_after 內有 2 筆以上才開
  attach  ：baseline status warning（只跳一次）：只併進已經開著的那件，不會開新的
同一個服務已經有開著的那件、而且離它最後一筆異常不到 close_after：併進去；超過 close_after 沒有新的異常：resolved。

跨服務（一層父子關係，docs/pdca/phase3-locate/2026-10-cross-service-aggregation.md）：calls 是這段時間 trace 算出來的
「上游 → 下游」（scenario/it/chain.py，用根因分析同一套 topology）。下游出事連帶上游異常時只開一件、留下游那件：
  - 上游服務的異常、自己沒有開著的件、它直接呼叫的下游有開著的件、時間重疊（離下游那件開始不到 close_after）→ 併進下游那件，
    下游那件的 upstream_services 記這個上游；之後同一個上游的異常照同服務的規則併（離最後一筆不到 close_after）。
  - 下游開新的一件時，直接呼叫它的上游若已經有一件、開始時間差不到 close_after → 上游那件整件併進下游：這一輪才開的直接消失；
    上一輪已寫進 ES 的標 resolved、merged_into 指到下游那件（Grafana 看到上游那件恢復、下游那件開）。
  - 只看直接呼叫（一層）：A→B→C 時 B 併進 C，A 不併進 C；已經帶 upstream_services 的件不會再被併進別件。
  - 沒有呼叫關係、或時間不重疊：照舊各開一件。

這裡只做「哪筆異常歸哪件」的演算法（單元測試用）；動一件屬性的在 core/entity/alert.Alert；讀寫 ES、跑根因分析在 alert_service.py。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from aiops.core.entity.alert import SEVERITY, Alert  # noqa: F401  SEVERITY 給 alert_service 用
from aiops.core.entity.anomaly import NODE_KEYS, NS_KEYS, entity_of as _entity_of  # noqa: F401  名字留給既有的 import
from aiops.scenario import Scenario, load_scenario

_ts = Alert._ts


def entity_of(a: dict, sc: Scenario | None = None) -> tuple[str, str]:
    """(類別, 名稱)：這筆異常歸到哪一件（core/entity/anomaly.entity_of；對象的 key 由情境給）。sc 沒給就依環境變數載入。"""
    return _entity_of(a, (sc or load_scenario()).entity_keys)


def kind_of(a: dict) -> str:
    """open／weak／attach。"""
    level = a.get("level")
    if level == "quality":
        return "open"
    if level in ("rule", "peer"):
        return "open" if a.get("severity") == "critical" else "weak"
    return "attach" if a.get("status") == "warning" else "open"   # baseline、trace baseline


def route_of(keys: dict | None, sc: Scenario | None = None) -> str | None:
    """「哪支 API」：IT 是 trace 入口的名稱或 method＋route，工廠是 recipe（情境決定；根因分析挑樣本也用它）。"""
    return (sc or load_scenario()).route_of(keys or {})


def _open_copy(al) -> Alert:
    """開著的件（ES 讀回來的 dict，或已經是 Alert）複製一份再改，不動原來那份。"""
    return Alert.from_doc(al) if isinstance(al, dict) else al.model_copy(deep=True)


def _call_graph(calls) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """{(上游, 下游)} → (每個服務直接呼叫的下游, 每個服務的直接上游)。"""
    down_of: dict[str, set[str]] = {}
    up_of: dict[str, set[str]] = {}
    for u, d in calls:
        if u != d:
            down_of.setdefault(u, set()).add(d)
            up_of.setdefault(d, set()).add(u)
    return down_of, up_of


def _downstream_alert(svc: str, t: datetime, by_ent: dict, down_of: dict, close_after: timedelta) -> Alert | None:
    """svc 直接呼叫的下游裡，有開著的件、時間重疊的那件（有好幾件取開始時間最近的）。"""
    cands = []
    for d in down_of.get(svc, ()):
        al = by_ent.get(("service", d))
        if not al:
            continue
        joined = svc in al.upstream_services
        if joined and t - al.updated_at <= close_after and t >= al.started_at - close_after:
            cands.append((timedelta(0), al))                      # 已經連帶過：照同服務的規則
        elif not joined and abs(t - al.started_at) <= close_after:
            cands.append((abs(t - al.started_at), al))            # 第一次：離下游那件開始不到 close_after
    return min(cands, key=lambda c: c[0])[1] if cands else None


def _upstream_station(a: dict, ent: tuple, links: dict, by_ent: dict, close_after: timedelta) -> Alert | None:
    """工廠：這批 lot 經過的前站有開著的件 → 併進前站那件。先找「那件已經有這批 lot」的前站（lot 在前站就異常了，最有力）；
    沒有就取離它最近、而且 lot 在前站的時間落在那件範圍內（前後各 close_after）的前站。"""
    lot = str((a.get("keys") or {}).get("lot"))
    cands = [(name, at, by_ent.get(("service", name))) for name, at in links[a["_id"]] if name != ent[1]]
    cands = [(n_, at, c) for n_, at, c in cands if c]
    al = next((c for _, _, c in cands if lot in c.lots), None)
    if al is None:
        al = next((c for _, at, c in cands if c.started_at - close_after <= _ts(at) <= c.updated_at + close_after), None)
    return al


def _open_new(ent: tuple, a: dict, kind: str, pending: dict, close_after: timedelta, sc: Scenario) -> Alert | None:
    """這筆異常要不要開新的一件：open 直接開（連同之前 pending 的 weak）；weak 要同對象 close_after 內有 2 筆以上；attach 不開。"""
    if kind == "attach":
        return None
    t = _ts(a["@timestamp"])
    if kind == "weak":
        pend = [p for p in pending.get(ent, []) if t - _ts(p["@timestamp"]) <= close_after] + [a]
        pending[ent] = pend
        if len(pend) < 2:
            return None
        first, rest = pend[0], pend
    else:
        first, rest = a, [*pending.pop(ent, []), a]
    al = Alert.open_for(*ent, first)
    for p in sorted(rest, key=lambda p: _ts(p["@timestamp"])):
        al.attach(p, sc.route_of(p.get("keys")))
    pending.pop(ent, None)
    return al


def _fold_upstreams(ent: tuple, al: Alert, up_of: dict, by_ent: dict, out: dict, now: datetime, close_after: timedelta) -> None:
    """下游開新的一件時：直接呼叫它的上游已經有一件、同時開始 → 上游那件整件併進來。"""
    for u in sorted(up_of.get(ent[1], ())):
        up = by_ent.get(("service", u))
        if up and not up.upstream_services and abs(up.started_at - al.started_at) <= close_after:
            up.fold_into(al, now)
            del by_ent[("service", u)]
            if up.new:
                del out[up.alert_id]


def merge(open_alerts: list, anomalies: list[dict], now: datetime, close_after: timedelta,
          done_ids: set | frozenset = frozenset(), calls: set | frozenset = frozenset(),
          links: dict[str, list[tuple[str, datetime]]] | None = None, scenario: Scenario | None = None) -> list[Alert]:
    """open_alerts：aiops-alerts 裡 status open 的；anomalies：最近的異常（含 _id）；
    done_ids：最近已經結束的件裡的異常 id（不要再被開一次，重跑才不會重複）；
    calls：{(上游服務, 下游服務)}，這段時間 trace 裡誰呼叫誰（沒有就不併跨服務）；
    links：工廠的前後站（scenario/factory/chain.lot_links）：{異常 _id: [(前站機台, 這批 lot 在前站的時間), …]}。
      後站（量測站）看到的異常是前站做壞的：前站有開著的件、而且 lot 在前站的時間落在那件的範圍內（前後各 close_after）→
      併進前站那件、記 upstream_services（跟 IT 的「上游併進下游」同一個欄位：被併進去的那一方）；自己已經有件就照同對象的規則。
    scenario：對象的 key、「哪支 API」怎麼算；沒給就依環境變數載入。
    回傳這次要寫回的件：新開的（_new）、有更新的、這次結束的（resolved，含被併進下游的 merged_into），以及其他還開著的（更新心跳）。"""
    links, sc = links or {}, scenario or load_scenario()
    by_ent = {(al.category, al.service): al for al in map(_open_copy, open_alerts)}
    down_of, up_of = _call_graph(calls)
    seen = {i for al in by_ent.values() for i in al.anomaly_ids} | set(done_ids)
    pending: dict[tuple, list[dict]] = {}
    out: dict[str, Alert] = {al.alert_id: al for al in by_ent.values()}
    for a in sorted(anomalies, key=lambda a: _ts(a["@timestamp"])):
        if a["_id"] in seen:
            continue
        seen.add(a["_id"])
        ent, t, k = entity_of(a, sc), _ts(a["@timestamp"]), kind_of(a)
        al = by_ent.get(ent)
        if al and t - al.updated_at > close_after:      # 隔太久：舊的那件結束，這筆當新的看
            al.close(al.updated_at + close_after)
            del by_ent[ent]
            al = None
        if al and t < al.started_at - close_after:   # 比這件開始還早很多：屬於更早的那件，不併
            continue
        if al:
            al.attach(a, sc.route_of(a.get("keys")))
            continue
        if ent[0] == "service":
            al = _downstream_alert(ent[1], t, by_ent, down_of, close_after)
            if al is None and a["_id"] in links:
                al = _upstream_station(a, ent, links, by_ent, close_after)
            if al:                                             # 上游（或前站）的異常：併進下游那件
                al.attach(a, sc.route_of(a.get("keys")))
                al.join_upstream(ent[1])
                continue
        al = _open_new(ent, a, k, pending, close_after, sc)
        if al is None:
            continue
        if ent[0] == "service":
            _fold_upstreams(ent, al, up_of, by_ent, out, now, close_after)
        by_ent[ent] = al
        out[al.alert_id] = al
    for al in by_ent.values():
        if now - al.updated_at > close_after:
            al.close(now)
    return list(out.values())


def _own(al: Alert, sc: Scenario | None = None) -> list[dict]:
    return al.own_anomalies((sc or load_scenario()).entity_keys)


def rca_route(al: Alert, sc: Scenario | None = None) -> str | None:
    sc = sc or load_scenario()
    return al.rca_route(sc.route_of, sc.entity_keys)


def owner(al: Alert, sc: Scenario | None = None) -> tuple[str | None, str]:
    return al.owner((sc or load_scenario()).entity_keys)


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


def link_shared(alerts: list[Alert]) -> set[str]:
    """開著的、不同服務的件卡在同一個外部依賴（同一個 Oracle）→ 互相記 related_alerts、shared_dependency。
    不合併：是不同服務各自的件（部門、處置不同），只提示很可能同源。對象是開著的件的服務時不算（那是上下游，chain 處理）。
    回傳 related_alerts 有變的 alert_id（摘要要重寫）。"""
    opens = [al for al in alerts if al.status == "open" and al.category == "service" and not al.merged_into]
    svcs = {al.service for al in opens}
    for al in opens:
        al.dependencies = [d for d in dependencies(al.findings) if d not in svcs]
    changed = set()
    for al in opens:
        rel, shared = [], []
        for other in opens:
            if other is al or other.service == al.service:
                continue
            common = [d for d in al.dependencies if d in other.dependencies]
            if common:
                rel.append(other.alert_id)
                shared += [c for c in common if c not in shared]
        rel = sorted(rel)
        if rel != sorted(al.related_alerts):
            changed.add(al.alert_id)
        al.related_alerts, al.shared_dependency = rel, shared
    return changed


def headline(al: Alert, count: bool = True, sc: Scenario | None = None) -> str:
    return al.headline((sc or load_scenario()).entity_keys, count)


conclusion = Alert.conclusion


def summary(al: Alert, report: str | None, tz, similar: list[dict] | None = None, sc: Scenario | None = None) -> str:
    return al.make_summary(report, tz, similar, (sc or load_scenario()).entity_keys)

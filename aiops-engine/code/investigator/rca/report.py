"""根因分析報告：一段一段（Section）組起來，哪幾段、什麼順序由 plugin 決定（core/plugin.py 的 sections）。

  anomalies   Metrics：這段時間這個對象的異常（⑤ aiops-anomalies）
  entries     Trace／履歷：這個對象收到的單位數、p95、趨勢；順便取樣慢的單位與平常的單位（後面幾段用）。沒有 trace 時只記一行，後面要 trace 的段各自略過
  segments    慢在哪一段（M4：每段自己花的時間跟平常比）＋錯誤最早出在哪一段＋慢的那批單位的共同點（commonality）
  slowest     最慢那個單位的瀑布圖
  logs        那幾段的 warn／error log 新樣式；最慢那個單位的 log
  changes     事故前 N 內的變更（PM、recipe、部署…）：cfg 有 changes.query 才出
  topology    這些單位經過的對象（mermaid）
  conclusion  結論（給人確認）；事件整合的 summary 從這段抓（alerts.conclusion 認「## 結論」）

每段只讀寫 Ctx：ES 查到的東西放 Ctx 上給後面的段用，findings 是給事件整合、相似事故、LLM 的結構化結果（key 不隨段的有無而變）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from core import plugin
from sources import spans_of
from sources import traces as T

from . import analyze as A
from . import queries as E

PAD = timedelta(minutes=5)


# ── 格式 ──

def _dur(s: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([smhdw])", s)
    if not m:
        raise SystemExit(f"時間長度看不懂：{s}（例如 30m、1h、7d）")
    n, u = int(m[1]), m[2]
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[u]: n})


def _ts(s: str | None) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else datetime.now(timezone.utc)


def _local(t: datetime, tz: str) -> str:
    from zoneinfo import ZoneInfo
    return t.astimezone(ZoneInfo(tz)).strftime("%m-%d %H:%M")


def _ms(v: float) -> str:
    """毫秒 → 給人看：工廠的一站是幾十分鐘，超過 1 分鐘用分鐘寫。"""
    if v >= 60_000:
        return f"{v / 60_000:.1f} 分"
    return f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms" if v >= 10 else f"{v:.1f} ms"


def _delta(v: float) -> str:
    return ("+" if v >= 0 else "-") + _ms(abs(v))


def _svc_of(keys: dict) -> str | None:
    """異常是哪個對象（服務／機台）：keys 裡哪幾個欄位算，由 plugin 決定（跟事件整合 alerts.entity_of 同一套）。"""
    for k in plugin.current().entity_keys:
        if keys.get(k):
            return str(keys[k])
    return None


def _route_of(keys: dict) -> str | None:
    """哪支 API（IT：trace 裡 Server span 的名稱；工廠：recipe）。plugin 決定。"""
    return plugin.current().route_of(keys)


def _trust(r: dict) -> str:
    """異常用的 baseline 可不可信：可信度、粒度、方法、借誰的。規則、跟兄弟比沒有這些欄位。"""
    if not r.get("confidence"):
        return ""
    src = r.get("baseline_source") or "self"
    extra = [r.get("resolution") or "", "卜瓦松" if r.get("method") == "poisson" else "",
             f"借 {src[len('group:'):]}" if src.startswith("group:") else ""]
    return r["confidence"] + "".join(f"、{x}" for x in extra if x)


def pick_from_anomalies(es, cfg, start, end):
    """沒指定對象：從 ⑤ 的異常裡，挑這段時間最嚴重的對象（偏離倍數最大），時間範圍取它異常的那段。
    異常帶著 API（method、route；工廠是 recipe）的話，也只查那支。"""
    rows = E.anomalies(es, cfg["indices"]["anomalies"], start, end)
    # warning（只跳一次、或即時檢查的）不觸發根因分析；沒有 status 的是規則、跟兄弟比，照算
    rows = [r for r in rows if _svc_of(r.get("keys") or {}) and r.get("status", "anomaly") == "anomaly"]
    if not rows:
        return None
    top = max(rows, key=lambda r: abs(r.get("z") or 0))
    svc, route = _svc_of(top["keys"]), _route_of(top["keys"])
    mine = [r for r in rows if _svc_of(r["keys"]) == svc]
    t = sorted(_ts(r["@timestamp"]) for r in mine)
    # 異常的時間是那 5 分鐘的開始，所以查到最後一個異常再往後 5 分鐘
    return svc, route, t[0], t[-1] + _dur(cfg.get("interval", "5m")), mine


# ── 一份報告的狀態 ──

class Ctx:
    def __init__(self, es, cfg: dict, service: str, route: str | None, start: datetime, end: datetime, found: list[dict]):
        self.es, self.cfg, self.ix = es, cfg, cfg["indices"]
        self.service, self.route, self.start, self.end, self.found = service, route, start, end, found
        self.tz = cfg.get("timezone", "Asia/Taipei")
        self.n = cfg.get("sample", 200)
        self.lag = _dur(str(cfg.get("entry_lag", "0s")))     # 入口比文件時間晚最多多久（工廠的履歷：lot 開始到到這一站；IT 是 0）
        self.U, self.TRACE = plugin.unit_text(), plugin.trace_label()  # 一個「請求」還是一個「 lot 」；trace 還是履歷
        self.what = f"{service}" + (f"（{route}）" if route else "")
        self.ref_start: datetime | None = None
        self.ref_end: datetime | None = None
        self.ref_label = ""
        self.has_traces: bool | None = None                   # None＝沒看（這個 plugin 沒有 entries 段）
        self.now_tr: dict[str, list[dict]] = {}
        self.ref_tr: dict[str, list[dict]] = {}
        self.diffs: list = []
        self.errs: list = []
        self.top: list = []
        self.commons: list = []
        self.trend: dict = {}
        self.span_label = ""
        self.pats: list = []
        self.changes: list[dict] = []
        self.suspects: list[str] = [service]
        self.out: list[str] = []
        self._n = 0
        self.findings: dict = {"service": service, "route": route, "start": start.isoformat(), "end": end.isoformat(), "ref_label": None,
                               "anomalies": [], "segments": [], "error_origins": [], "new_patterns": [], "topology": [],
                               "requests": None, "trend": None, "commonality": [], "changes": []}

    def heading(self, title: str) -> None:
        """「## N. 標題」：編號照出現的順序，沒出的段不占號。"""
        self._n += 1
        self.out += [f"## {self._n}. {title}", ""]

    def local(self, t) -> str:
        return _local(t if isinstance(t, datetime) else _ts(str(t)), self.tz)

    @property
    def traced(self) -> bool:
        return bool(self.has_traces and self.now_tr)


def _skip(c: Ctx, title: str, why: str) -> None:
    c.heading(title)
    c.out += [why, ""]


def pick_reference(c: Ctx, need_traces: bool) -> None:
    """「平常」是哪一段：一週前同一段時間；那時候沒資料（例如服務剛上線）就用出事前那一段（同樣長，空 ref_gap 避開剛開始變壞的時候）。
    要 trace 的 plugin 看那段有沒有請求來挑；只有指標的沒得挑，就用一週前。"""
    cfg = c.cfg
    back, length, gap = _dur(cfg.get("compare_with", "7d")), c.end - c.start, _dur(cfg.get("ref_gap", "10m"))
    cands = ((c.start - back, c.end - back, f"{cfg.get('compare_with', '7d')} 前同一段時間"),
             (c.start - gap - length, c.start - gap, "出事前一段時間"))
    for rs, re_, label in cands:
        if not need_traces or E.entry_stats(c.es, c.ix["traces"], c.service, c.route, rs, re_, c.lag)["requests"]:
            c.ref_start, c.ref_end, c.ref_label = rs, re_, label
            break
    c.findings["ref_label"] = c.ref_label or None


# ── 段 ──

def sec_anomalies(c: Ctx) -> None:
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
        c.out.append(f"這段時間 ⑤ 沒有記到這個{plugin.entity_label()}的異常（直接指定{plugin.entity_label()}查）。")
    c.out.append("")


def sec_entries(c: Ctx) -> None:
    """這個對象收到的單位：數量、錯誤、p50／p95 跟平常比；出事前的趨勢；取樣最慢的那批與平常的那批。"""
    es, ix, U, TRACE = c.es, c.ix, c.U, c.TRACE
    now = E.entry_stats(es, ix["traces"], c.service, c.route, c.start, c.end, c.lag)
    c.heading(f"{TRACE}：這個{plugin.entity_label()}收到的{U}")
    c.findings["requests"] = now
    if not now["requests"]:
        c.has_traces = False
        c.out += [f"這段時間沒有這個{plugin.entity_label()}的{TRACE}（沒埋點、名稱不對、或{TRACE}還沒合成完；合成晚 1～2 分鐘）。"
                  f"先從 log 找；沒有{TRACE}的件系統會自動重跑報告（每 2 分鐘、最多 5 次）。", ""]
        return
    c.has_traces = True
    ref = E.entry_stats(es, ix["traces"], c.service, c.route, c.ref_start, c.ref_end, c.lag) if c.ref_start else None
    c.out += [f"| | 現在 | 平常（{c.ref_label or '沒有資料'}） |", "|---|---|---|"]
    for k, label in (("requests", f"{U}數（{TRACE}，有取樣）"), ("errors", "錯誤"), ("p50_ms", "p50"), ("p95_ms", "p95")):
        f = _ms if k.endswith("_ms") else str
        c.out.append(f"| {label} | {f(now[k])} | {f(ref[k]) if ref else '—'} |")
    c.out.append("")
    # 出事前的趨勢：前面幾段（預設 6 段、每段 10 分鐘）的 p95，一路往上＝慢慢惡化、平的然後跳＝突然
    tcfg = c.cfg.get("trend") or {}
    tw, tn = _dur(str(tcfg.get("window", "10m"))), int(tcfg.get("windows", 6))
    p95s = [E.entry_stats(es, ix["traces"], c.service, c.route, c.start - tw * (k + 1), c.start - tw * k, c.lag)["p95_ms"] for k in reversed(range(tn))]
    c.trend = A.classify_trend(p95s, now["p95_ms"])
    c.trend["window"], c.trend["windows"] = str(tcfg.get("window", "10m")), tn
    c.findings["trend"] = c.trend
    c.span_label = f"{int(tw.total_seconds() * tn // 60)} 分鐘"
    c.out += [A.describe_trend(c.trend, c.span_label, _ms), ""]
    now_ids = E.sample_trace_ids(es, ix["traces"], c.service, c.route, c.start, c.end, c.n, slowest=True, lag=c.lag)
    c.now_tr = T.group_traces(spans_of(es, ix["traces"], now_ids, c.start - PAD - c.lag, c.end + PAD))
    if c.ref_start:
        ref_ids = E.sample_trace_ids(es, ix["traces"], c.service, c.route, c.ref_start, c.ref_end, c.n, slowest=False, lag=c.lag)
        c.ref_tr = T.group_traces(spans_of(es, ix["traces"], ref_ids, c.ref_start - PAD - c.lag, c.ref_end + PAD))


def sec_segments(c: Ctx) -> None:
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
    ccfg = c.cfg.get("commonality") or {}
    commons = A.commonality(c.now_tr, c.ref_tr, res_keys=tuple(ccfg.get("resource") or ("k8s.pod.name", "service.version", "k8s.node.name")),
                            attr_keys=tuple(ccfg.get("attrs") or ("lot.id", "db.statement")),
                            min_share=float(ccfg.get("min_share", 0.6)), min_lift=float(ccfg.get("min_lift", 2)),
                            min_traces=int(ccfg.get("min_traces", 5)))
    c.commons = commons
    c.findings["commonality"] = [{"dim": x.dim, "value": x.value, "now": x.now, "now_share": x.now_share, "ref_share": x.ref_share}
                                 for x in commons[:5]]
    if commons:
        c.out += [f"這批慢的{U}有什麼共同點（平常沒這麼集中）：", "", f"| 看什麼 | 值 | 慢的{U}裡 | 平常 |", "|---|---|---|---|"]
        c.out += [f"| {x.dim} | `{x.value}` | {x.now_share:.0%}（{x.now} 個） | {'平常沒見過（新的？）' if x.is_new else f'{x.ref_share:.0%}'} |"
                  for x in commons[:5]]
        c.out.append("")
    c.suspects = list(dict.fromkeys([d.segment.split(" › ")[0] for d in c.top + errs[:3]] + [c.service]))


def _slowest(c: Ctx) -> list[dict] | None:
    return max(c.now_tr.values(), key=lambda t: A.root_of(t)["dur_ms"]) if c.now_tr else None


def sec_slowest(c: Ctx) -> None:
    U = c.U
    sl = _slowest(c) if c.traced else None
    if not sl:
        return _skip(c, f"最慢的那個{U.strip()}", f"（沒有{c.TRACE}，略過）")
    idl = "lot" if U.strip() == "lot" else "trace_id"
    c.heading(f"最慢的那個{U}（{idl} `{sl[0]['trace_id']}`）")
    c.out += ["```"] + A.waterfall(sl) + ["```", ""]


def sec_logs(c: Ctx) -> None:
    es, ix, U = c.es, c.ix, c.U
    c.heading("Log：那幾段發生了什麼事")
    c.out += [f"看 {', '.join(c.suspects)} 這段時間的 warn／error，跟{c.ref_label or '平常'}比；新出現的排前面：", ""]
    p_now = E.problem_logs(es, ix["logs"], c.suspects, c.start, c.end)
    p_ref = E.problem_logs(es, ix["logs"], c.suspects, c.ref_start, c.ref_end) if c.ref_start else []
    pats = A.compare_patterns(p_now, p_ref)
    c.pats = pats
    c.findings["new_patterns"] = [{"service": p.service, "pattern": p.pattern[:200], "now": p.now, "ref": p.ref, "is_new": bool(p.is_new),
                                   "example": p.example.splitlines()[0][:200]} for p in pats[:8]]
    if pats:
        c.out += [f"| {plugin.entity_label()} | 樣式 | 現在 | 平常 | 例子 |", "|---|---|---|---|---|"]
        for p in pats[:8]:
            ex = p.example.splitlines()[0][:120].replace("|", "\\|")
            c.out.append(f"| {p.service} | {'🆕 ' if p.is_new else ''}`{p.pattern[:80].replace('|', '/')}` | {p.now} | {p.ref} | {ex} |")
    else:
        c.out.append("沒有比平常多的 warn／error log。")
    sl = _slowest(c) if c.traced else None
    if sl:
        lines = E.logs_of_traces(es, ix["logs"], [sl[0]["trace_id"]], c.start - PAD, c.end + PAD)
        idl = "lot id" if U.strip() == "lot" else "trace_id"
        c.out += ["", f"最慢那個{U}的 log（依 {idl} 撈出來，{len(lines)} 行）：", "", "```"]
        c.out += [f"{l['ts']}  {l['service']:<14} {str(l['level'] or ''):<5}  {l['message'].splitlines()[0][:150]}" for l in lines[:20]] \
            or [f"（沒有：這幾個{plugin.entity_label()}的 log 沒寫 {idl}？）"]
        c.out.append("```")
    c.out.append("")


def sec_changes(c: Ctx) -> None:
    """事故前的變更（PM、recipe 變更、部署、設定）：出事前 lookback 內、這幾個對象的變更紀錄，照時間列。cfg 有 changes.query 才出。"""
    chcfg = c.cfg.get("changes") or {}
    if not chcfg.get("query"):
        return
    lookback = str(chcfg.get("lookback", "12h"))
    c.heading(f"事故前 {lookback} 內的變更")
    try:
        c.changes = E.change_logs(c.es, c.ix["logs"], c.suspects, c.start - _dur(lookback), c.end, chcfg["query"])
    except Exception as ex:  # noqa: BLE001  變更紀錄查不到不擋報告
        c.out += [f"（事故前的變更查不到：{type(ex).__name__}）", ""]
        return
    c.findings["changes"] = [{"at": x["ts"], "service": x["service"], "message": x["message"].splitlines()[0][:200]} for x in c.changes[:10]]
    c.out += [f"- {c.local(str(x['ts']))}  {x['service']}  {x['message'].splitlines()[0][:150]}" for x in c.changes[:10]] or ["沒有變更紀錄。"]
    c.out.append("")


def sec_topology(c: Ctx) -> None:
    if not c.traced:
        return _skip(c, f"這些{c.U}經過的{plugin.entity_label()}", f"（沒有{c.TRACE}，略過）")
    edges = A.topology(c.now_tr)
    c.findings["topology"] = [{"caller": e.caller, "callee": e.callee, "calls": e.calls, "errors": e.errors} for e in edges]
    c.heading(f"這些{c.U}經過的{plugin.entity_label()}")
    c.out += ["```mermaid", A.mermaid(edges), "```", ""]


def sec_conclusion(c: Ctx) -> None:
    U = c.U
    c.out += ["## 結論（給人確認）", ""]
    if c.top:
        d = c.top[0]
        c.out.append(f"- {c.what} 變慢，主要多在 **{d.segment}**：每個{U}多花 {_ms(d.delta_ms)}（平常 {_ms(d.ref_ms)}）。")
    if c.errs:
        c.out.append(f"- 錯誤最早出在 **{c.errs[0].segment}**（{c.errs[0].now_errors} 個{U.rstrip()}，平常 {c.errs[0].ref_errors}）。")
    new = [p for p in c.pats if p.is_new][:2]
    for p in new:
        c.out.append(f"- 同時間 {p.service} 新出現 log「{p.example.splitlines()[0][:100]}」，{p.now} 次。")
    if c.commons:
        x = c.commons[0]
        if x.is_new:
            c.out.append(f"- 慢的{U} {x.now_share:.0%} 都在平常沒見過的 {x.dim} `{x.value}`：平常那批完全沒有這個值（{plugin.new_value_hint()}），先確認那時有沒有換。")
        else:
            c.out.append(f"- 慢的{U} {x.now_share:.0%} 都是 {x.dim} = `{x.value}`（平常 {x.ref_share:.0%}）：問題可能只在它身上。")
    if c.trend.get("kind") in ("gradual", "sudden"):
        c.out.append("- " + A.describe_trend(c.trend, c.span_label, _ms).split("→ ", 1)[1].replace("**", ""))
    if c.changes:
        x = c.changes[0]
        c.out.append(f"- 出事前 {x['service']} 有變更：{c.local(str(x['ts']))}「{x['message'].splitlines()[0][:80]}」，先確認是不是它。")
    if not (c.top or c.errs or new):
        if c.has_traces is False:
            c.out.append(f"- 沒有{c.TRACE}可以定位哪一段；只看得到 ⑤ 的異常與 log。")
        elif c.has_traces is None:
            c.out.append(f"- 這個設定沒有{c.TRACE}：只看 ⑤ 的異常、log、事故前的變更，定位到哪一段要有{c.TRACE}。")
        else:
            c.out.append(f"- 跟平常比看不出哪一段變化特別大，可能是整體量變大，或問題不在有{c.TRACE}的地方。")


SECTIONS: dict[str, Callable[[Ctx], None]] = {
    "anomalies": sec_anomalies, "entries": sec_entries, "segments": sec_segments, "slowest": sec_slowest,
    "logs": sec_logs, "changes": sec_changes, "topology": sec_topology, "conclusion": sec_conclusion,
}
TRACE_SECTIONS = ("entries", "segments", "slowest", "topology")


def investigate_full(cfg: dict, args, sections: tuple[str, ...] | None = None) -> tuple[str, dict]:
    """回傳 (Markdown 報告, findings)。findings 是結構化的結果：異常、慢在哪一段、錯從哪開始、新 log 樣式、呼叫關係，
    給事件整合存進 aiops-alerts、相似事故檢索（investigator/similar/similar.py）和 LLM 摘要（core/llm.py）用。
    sections 不給就用 plugin 的（core/plugin.py）。"""
    es = E.ES()
    names = tuple(sections or plugin.current().sections)
    unknown = [s for s in names if s not in SECTIONS]
    if unknown:
        raise ValueError(f"不認得的報告段：{unknown}；有 {list(SECTIONS)}")
    ix, tz = cfg["indices"], cfg.get("timezone", "Asia/Taipei")
    service, route, found = args.service, args.route, []
    if args.start:
        start, end = _ts(args.start), _ts(args.end)
    else:
        end = _ts(args.at)
        start = end - _dur(args.window)
    if not service:
        picked = pick_from_anomalies(es, cfg, start, end)
        if not picked:
            empty = {"service": None, "route": None, "anomalies": [], "segments": [], "error_origins": [], "new_patterns": [],
                     "topology": [], "requests": None, "ref_label": None, "trend": None, "commonality": [], "changes": []}
            return f"{_local(start, tz)}～{_local(end, tz)} 沒有 ⑤ 的異常。要查特定{plugin.entity_label()}的話用 --service。", empty
        service, route, start, end, found = picked
    else:
        found = E.anomalies(es, ix["anomalies"], start, end, service)
    c = Ctx(es, cfg, service, route, start, end, found)
    pick_reference(c, need_traces=any(s in names for s in TRACE_SECTIONS))
    c.out = [f"# 根因分析：{c.what}，{_local(start, tz)}～{_local(end, tz)}（{tz}）", ""]
    for s in names:
        SECTIONS[s](c)
    return "\n".join(c.out), c.findings

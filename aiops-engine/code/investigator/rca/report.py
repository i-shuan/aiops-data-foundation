"""根因分析報告：一段一段（Section）組起來，哪幾段、什麼順序由 plugin 決定（core/plugin.py 的 sections、report_sections()）。

這裡只有框架（Ctx、挑「平常」、組報告）與不分情境的四段：

  anomalies   Metrics：這段時間這個對象的異常（⑤ aiops-anomalies）
  logs        可疑對象的 warn／error log 新樣式；最慢那個單位的 log（有 path 才有）
  changes     事故前 N 內的變更（PM、recipe、部署…）：cfg 有 changes.query 才出
  conclusion  結論（給人確認）；事件整合的 summary 從這段抓（alerts.conclusion 認「## 結論」）

有 path（trace／履歷）的情境另外有 entries、segments、slowest、topology 四段，在 path_sections.py，由 plugin 的 report_sections() 加進來。
每段只讀寫 Ctx：ES 查到的東西放 Ctx 上給後面的段用，findings 是給事件整合、相似事故、LLM 的結構化結果（key 不隨段的有無而變）。
findings 與報告裡的欄位名（service、route、trace_id…）是 aiops-alerts 文件的契約，照舊；程式裡的識別字用 entity／where／path。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from core import plugin

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


def _entity_of(keys: dict) -> str | None:
    """異常是哪個對象（服務／機台）：keys 裡哪幾個欄位算，由 plugin 決定（跟事件整合 alerts.entity_of 同一套）。"""
    for k in plugin.current().entity_keys:
        if keys.get(k):
            return str(keys[k])
    return None


def _where_of(keys: dict) -> str | None:
    """異常在哪裡（IT：哪支 API；工廠：哪個 recipe）。plugin 決定。"""
    return plugin.current().where_of(keys)


def _trust(r: dict) -> str:
    """異常用的 baseline 可不可信：可信度、粒度、方法、借誰的。規則、同儕比對沒有這些欄位。"""
    if not r.get("confidence"):
        return ""
    src = r.get("baseline_source") or "self"
    extra = [r.get("resolution") or "", "卜瓦松" if r.get("method") == "poisson" else "",
             f"借 {src[len('group:'):]}" if src.startswith("group:") else ""]
    return r["confidence"] + "".join(f"、{x}" for x in extra if x)


def pick_from_anomalies(es, cfg, start, end):
    """沒指定對象：從 ⑤ 的異常裡，挑這段時間最嚴重的對象（偏離倍數最大），時間範圍取它異常的那段。
    異常帶著「在哪裡」（IT 的 method、route；工廠的 recipe）的話，也只查那一支。"""
    rows = E.anomalies(es, cfg["indices"]["anomalies"], start, end)
    # warning（只跳一次、或即時檢查的）不觸發根因分析；沒有 status 的是規則、同儕比對，照算
    rows = [r for r in rows if _entity_of(r.get("keys") or {}) and r.get("status", "anomaly") == "anomaly"]
    if not rows:
        return None
    top = max(rows, key=lambda r: abs(r.get("z") or 0))
    ent, where = _entity_of(top["keys"]), _where_of(top["keys"])
    mine = [r for r in rows if _entity_of(r["keys"]) == ent]
    t = sorted(_ts(r["@timestamp"]) for r in mine)
    # 異常的時間是那 5 分鐘的開始，所以查到最後一個異常再往後 5 分鐘
    return ent, where, t[0], t[-1] + _dur(cfg.get("interval", "5m")), mine


# ── 一份報告的狀態 ──

class Ctx:
    def __init__(self, es, cfg: dict, entity: str, where: str | None, start: datetime, end: datetime, found: list[dict]):
        self.es, self.cfg, self.ix = es, cfg, cfg["indices"]
        self.entity, self.where, self.start, self.end, self.found = entity, where, start, end, found
        self.tz = cfg.get("timezone", "Asia/Taipei")
        self.n = cfg.get("sample", 200)
        self.lag = _dur(str(cfg.get("entry_lag", "0s")))     # 入口比文件時間晚最多多久（工廠的履歷：lot 開始到到這一站；IT 是 0）
        self.U, self.PATH = plugin.unit_text(), plugin.path_label()  # 一個「請求」還是一個「 lot 」；Trace 還是履歷
        self.what = f"{entity}" + (f"（{where}）" if where else "")
        self.ref_start: datetime | None = None
        self.ref_end: datetime | None = None
        self.ref_label = ""
        self.has_paths: bool | None = None                    # None＝沒看（這個 plugin 沒有 entries 段）
        self.now_tr: dict[str, list[dict]] = {}
        self.ref_tr: dict[str, list[dict]] = {}
        self.diffs: list = []
        self.errs: list = []
        self.top: list = []
        self.commons: list = []
        self.trend: dict = {}
        self.period_label = ""
        self.pats: list = []
        self.changes: list[dict] = []
        self.suspects: list[str] = [entity]
        self.out: list[str] = []
        self._n = 0
        self.findings: dict = {"service": entity, "route": where, "start": start.isoformat(), "end": end.isoformat(), "ref_label": None,
                               "anomalies": [], "segments": [], "error_origins": [], "new_patterns": [], "topology": [],
                               "requests": None, "trend": None, "commonality": [], "changes": []}

    def heading(self, title: str) -> None:
        """「## N. 標題」：編號照出現的順序，沒出的段不占號。"""
        self._n += 1
        self.out += [f"## {self._n}. {title}", ""]

    def local(self, t) -> str:
        return _local(t if isinstance(t, datetime) else _ts(str(t)), self.tz)

    @property
    def pathed(self) -> bool:
        """有 path 可以看（entries 段抽到了單位）。"""
        return bool(self.has_paths and self.now_tr)

    def slowest(self) -> list[dict] | None:
        return max(self.now_tr.values(), key=lambda t: A.root_of(t)["dur_ms"]) if self.pathed else None


def _skip(c: Ctx, title: str, why: str) -> None:
    c.heading(title)
    c.out += [why, ""]


def pick_reference(c: Ctx, probe: Callable[[datetime, datetime], bool] | None = None) -> None:
    """「平常」是哪一段：一週前同一段時間；那時候沒資料（例如服務剛上線）就用出事前那一段（同樣長，空 ref_gap 避開剛開始變壞的時候）。
    probe(start, end) 回那段有沒有資料：有 path 的段（path_sections.sec_entries）給它、看那段有沒有單位來挑；沒給（只有指標）就用一週前。"""
    cfg = c.cfg
    c.ref_start, c.ref_end, c.ref_label = None, None, ""
    back, length, gap = _dur(cfg.get("compare_with", "7d")), c.end - c.start, _dur(cfg.get("ref_gap", "10m"))
    cands = ((c.start - back, c.end - back, f"{cfg.get('compare_with', '7d')} 前同一段時間"),
             (c.start - gap - length, c.start - gap, "出事前一段時間"))
    for rs, re_, label in cands:
        if probe is None or probe(rs, re_):
            c.ref_start, c.ref_end, c.ref_label = rs, re_, label
            break
    c.findings["ref_label"] = c.ref_label or None


# ── 中立的段 ──

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


def sec_logs(c: Ctx) -> None:
    es, ix, U = c.es, c.ix, c.U
    c.heading("Log：那幾段發生了什麼事")
    c.out += [f"看 {', '.join(c.suspects)} 這段時間的 warn／error，跟{c.ref_label or '平常'}比；新出現的排前面：", ""]
    p_now = E.problem_logs(es, ix["logs"], c.suspects, c.start, c.end)
    p_ref = E.problem_logs(es, ix["logs"], c.suspects, c.ref_start, c.ref_end) if c.ref_start else []
    pats = A.compare_patterns(p_now, p_ref)
    c.pats = pats
    c.findings["new_patterns"] = [{"service": p.entity, "pattern": p.pattern[:200], "now": p.now, "ref": p.ref, "is_new": bool(p.is_new),
                                   "example": p.example.splitlines()[0][:200]} for p in pats[:8]]
    if pats:
        c.out += [f"| {plugin.entity_label()} | 樣式 | 現在 | 平常 | 例子 |", "|---|---|---|---|---|"]
        for p in pats[:8]:
            ex = p.example.splitlines()[0][:120].replace("|", "\\|")
            c.out.append(f"| {p.entity} | {'🆕 ' if p.is_new else ''}`{p.pattern[:80].replace('|', '/')}` | {p.now} | {p.ref} | {ex} |")
    else:
        c.out.append("沒有比平常多的 warn／error log。")
    sl = c.slowest()
    if sl:
        lines = E.logs_of_paths(es, ix["logs"], [sl[0]["trace_id"]], c.start - PAD, c.end + PAD)
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
        c.out.append(f"- 同時間 {p.entity} 新出現 log「{p.example.splitlines()[0][:100]}」，{p.now} 次。")
    if c.commons:
        x = c.commons[0]
        if x.is_new:
            c.out.append(f"- 慢的{U} {x.now_share:.0%} 都在平常沒見過的 {x.dim} `{x.value}`：平常那批完全沒有這個值（{plugin.new_value_hint()}），先確認那時有沒有換。")
        else:
            c.out.append(f"- 慢的{U} {x.now_share:.0%} 都是 {x.dim} = `{x.value}`（平常 {x.ref_share:.0%}）：問題可能只在它身上。")
    if c.trend.get("kind") in ("gradual", "sudden"):
        c.out.append("- " + A.describe_trend(c.trend, c.period_label, _ms).split("→ ", 1)[1].replace("**", ""))
    if c.changes:
        x = c.changes[0]
        c.out.append(f"- 出事前 {x['service']} 有變更：{c.local(str(x['ts']))}「{x['message'].splitlines()[0][:80]}」，先確認是不是它。")
    if not (c.top or c.errs or new):
        if c.has_paths is False:
            c.out.append(f"- 沒有{c.PATH}可以定位哪一段；只看得到 ⑤ 的異常與 log。")
        elif c.has_paths is None:
            c.out.append(f"- 這個設定沒有{c.PATH}：只看 ⑤ 的異常、log、事故前的變更，定位到哪一段要有{c.PATH}。")
        else:
            c.out.append(f"- 跟平常比看不出哪一段變化特別大，可能是整體量變大，或問題不在有{c.PATH}的地方。")


SECTIONS: dict[str, Callable[[Ctx], None]] = {
    "anomalies": sec_anomalies, "logs": sec_logs, "changes": sec_changes, "conclusion": sec_conclusion,
}


def investigate_full(cfg: dict, args, sections: tuple[str, ...] | None = None) -> tuple[str, dict]:
    """回傳 (Markdown 報告, findings)。findings 是結構化的結果：異常、慢在哪一段、錯從哪開始、新 log 樣式、呼叫關係，
    給事件整合存進 aiops-alerts、相似事故檢索（investigator/similar/similar.py）和 LLM 摘要（core/llm.py）用。
    args：entity（--service）、where（--route）、start／end 或 at＋window。sections 不給就用 plugin 的（core/plugin.py）。"""
    es = E.ES()
    pg = plugin.current()
    table = {**SECTIONS, **pg.report_sections()}        # 中立四段＋這個情境加的（有 path 的：path_sections）
    names = tuple(sections or pg.sections)
    unknown = [s for s in names if s not in table]
    if unknown:
        raise ValueError(f"不認得的報告段：{unknown}；有 {list(table)}")
    ix, tz = cfg["indices"], cfg.get("timezone", "Asia/Taipei")
    entity, where, found = args.entity, args.where, []
    if args.start:
        start, end = _ts(args.start), _ts(args.end)
    else:
        end = _ts(args.at)
        start = end - _dur(args.window)
    if not entity:
        picked = pick_from_anomalies(es, cfg, start, end)
        if not picked:
            empty = {"service": None, "route": None, "anomalies": [], "segments": [], "error_origins": [], "new_patterns": [],
                     "topology": [], "requests": None, "ref_label": None, "trend": None, "commonality": [], "changes": []}
            return f"{_local(start, tz)}～{_local(end, tz)} 沒有 ⑤ 的異常。要查特定{plugin.entity_label()}的話用 --service。", empty
        entity, where, start, end, found = picked
    else:
        found = E.anomalies(es, ix["anomalies"], start, end, entity)
    c = Ctx(es, cfg, entity, where, start, end, found)
    pick_reference(c)                                    # 先用一週前；有 path 的段（entries）會再依那段有沒有單位重挑
    c.out = [f"# 根因分析：{c.what}，{_local(start, tz)}～{_local(end, tz)}（{tz}）", ""]
    for s in names:
        table[s](c)
    return "\n".join(c.out), c.findings

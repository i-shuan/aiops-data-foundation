"""根因分析報告：一段一段（Section）組起來，哪幾段、什麼順序由情境決定（Scenario.sections）。

  anomalies   Metrics：這段時間這個對象的異常（⑤ aiops-anomalies）
  entries     Trace／履歷：這個對象收到的單位數、p95、趨勢；順便取樣慢的單位與平常的單位（後面幾段用）。沒有 trace 時只記一行，後面要 trace 的段各自略過
  segments    慢在哪一段（M4：每段自己花的時間跟平常比）＋錯誤最早出在哪一段＋慢的那批單位的共同點（commonality）
  slowest     最慢那個單位的瀑布圖
  logs        那幾段的 warn／error log 新樣式；最慢那個單位的 log
  changes     事故前 N 內的變更（PM、recipe、部署…）：rca_config 有 changes.query 才出
  topology    這些單位經過的對象（mermaid）
  conclusion  結論（給人確認）；事件整合的 summary 從這段抓（alerts.conclusion 認「## 結論」）

每段只讀寫 Ctx：ES 查到的東西放 Ctx 上給後面的段用，findings 是給事件整合、相似事故、LLM 的結構化結果（key 不隨段的有無而變）。
"""
from __future__ import annotations

import common.stages as ST

import pandas as pd

from common.clock import TimeWindow, now_of, parse_duration
from incident.config.rca import RcaConfig
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import data.store.models as T
from common.infra.es import ES
from detect.store.models import entity_value
from common.infra.repos import Repos
from common.scenario import load_scenario


PAD = timedelta(minutes=5)


# ── 格式 ──

def _ts(s: str | None) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else datetime.now(timezone.utc)


def _local(t: datetime, tz: str) -> str:
    return t.astimezone(ZoneInfo(tz)).strftime("%m-%d %H:%M")


def _ms(v: float) -> str:
    """毫秒 → 給人看：工廠的一站是幾十分鐘，超過 1 分鐘用分鐘寫。"""
    if v >= 60_000:
        return f"{v / 60_000:.1f} 分"
    return f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms" if v >= 10 else f"{v:.1f} ms"


def _delta(v: float) -> str:
    return ("+" if v >= 0 else "-") + _ms(abs(v))


def _svc_of(keys: dict, sc) -> str | None:
    """異常是哪個對象（服務／機台）：keys 裡哪幾個欄位算，由情境決定（跟事件整合 entity_of 同一套）。"""
    return entity_value(keys, sc.entity_keys)


def _trust(r: dict) -> str:
    """異常用的 baseline 可不可信：可信度、粒度、方法、借誰的。規則、跟兄弟比沒有這些欄位。"""
    if not r.get("confidence"):
        return ""
    src = r.get("baseline_source") or "self"
    extra = [r.get("resolution") or "", "卜瓦松" if r.get("method") == "poisson" else "",
             f"借 {src[len('group:'):]}" if src.startswith("group:") else ""]
    return r["confidence"] + "".join(f"、{x}" for x in extra if x)


def pick_from_anomalies(repo: Repos, rca_config: RcaConfig, start, end):
    """沒指定對象：從 ⑤ 的異常裡，挑這段時間最嚴重的對象（偏離倍數最大），時間範圍取它異常的那段。
    異常帶著 API（method、route；工廠是 recipe）的話，也只查那支。"""
    sc = repo.scenario
    rows = repo.anomalies.between(start, end, size=1000)
    # warning（只跳一次、或即時檢查的）不觸發根因分析；沒有 status 的是規則、同儕比對，照算
    rows = [r for r in rows if _svc_of(r.get("keys") or {}, sc) and r.get("status", "anomaly") == "anomaly"]
    if not rows:
        return None
    top = max(rows, key=lambda r: abs(r.get("z") or 0))
    svc, route = _svc_of(top["keys"], sc), sc.route_of(top["keys"])
    mine = [r for r in rows if _svc_of(r["keys"], sc) == svc]
    t = sorted(_ts(r["@timestamp"]) for r in mine)
    # 異常的時間是那 5 分鐘的開始，所以查到最後一個異常再往後 5 分鐘
    return svc, route, t[0], t[-1] + parse_duration(rca_config.interval), mine


# ── 一份報告的狀態 ──

class Ctx:
    def __init__(self, repo: Repos, rca_config: RcaConfig, service: str, route: str | None, start: datetime, end: datetime, found: list[dict]):
        self.repo, self.sc, self.rca_config, self.ix = repo, repo.scenario, rca_config, rca_config.indices
        self.service, self.route, self.start, self.end, self.found = service, route, start, end, found
        self.tz = rca_config.timezone
        self.n = rca_config.sample
        self.lag = parse_duration(rca_config.entry_lag)     # 入口比文件時間晚最多多久（工廠的履歷：lot 開始到到這一站；IT 是 0）
        self.U, self.TRACE = self.sc.unit_text(), self.sc.trace  # 一個「請求」還是一個「 lot 」；trace 還是履歷
        self.what = f"{service}" + (f"（{route}）" if route else "")
        self.ref_start: datetime | None = None
        self.ref_end: datetime | None = None
        self.ref_label = ""
        self.has_traces: bool | None = None                   # None＝沒看（這個情境沒有 entries 段）
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
    要 trace 的情境看那段有沒有請求來挑；只有指標的沒得挑，就用一週前。"""
    rca_config = c.rca_config
    back, length, gap = parse_duration(rca_config.compare_with), c.end - c.start, parse_duration(rca_config.ref_gap)
    cands = ((c.start - back, c.end - back, f"{rca_config.compare_with} 前同一段時間"),
             (c.start - gap - length, c.start - gap, "出事前一段時間"))
    for rs, re_, label in cands:
        if not need_traces or c.repo.traces.entry_stats(c.ix.traces, c.service, c.route, rs, re_, c.lag)["requests"]:
            c.ref_start, c.ref_end, c.ref_label = rs, re_, label
            break
    c.findings["ref_label"] = c.ref_label or None


# ── 段 ──


def _slowest(c: Ctx) -> list[dict] | None:
    return max(c.now_tr.values(), key=lambda t: T.root_of(t)["dur_ms"]) if c.now_tr else None


TRACE_SECTIONS = ("entries", "segments", "slowest", "topology")


def investigate_full(rca_config: RcaConfig, args, sections: tuple[str, ...] | None = None) -> tuple[str, dict]:
    """回傳 (Markdown 報告, findings)。findings 是結構化的結果：異常、慢在哪一段、錯從哪開始、新 log 樣式、呼叫關係，
    給事件整合存進 aiops-alerts、相似事故檢索（investigator/similar.py）和 LLM 摘要（llm/summary.py）用。
    sections 不給就用情境的（Scenario.sections）。"""
    repo = Repos(ES(), load_scenario())
    st = ST.stage("rca_sections")
    names = tuple(sections or st.enabled(repo.scenario))      # ⑦ 報告段落的外掛（plugins/plugin_rca；沒寫 blocks 用情境的 sections）
    unknown = [s for s in names if s not in st.builtin and ":" not in s]
    if unknown:
        raise ValueError(f"不認得的報告段：{unknown}；有 {list(st.builtin)}")
    tz = rca_config.timezone
    service, route, found = args.service, args.route, []
    if args.start:
        window = TimeWindow(pd.Timestamp(args.start), pd.Timestamp(args.end) if args.end else now_of())
    else:
        window = TimeWindow.ending(now_of(args.at), args.window)
    start, end = window.py()
    if not service:
        picked = pick_from_anomalies(repo, rca_config, start, end)
        if not picked:
            empty = {"service": None, "route": None, "anomalies": [], "segments": [], "error_origins": [], "new_patterns": [],
                     "topology": [], "requests": None, "ref_label": None, "trend": None, "commonality": [], "changes": []}
            return f"{_local(start, tz)}～{_local(end, tz)} 沒有 ⑤ 的異常。要查特定{repo.scenario.entity}的話用 --service。", empty
        service, route, start, end, found = picked
    else:
        found = repo.anomalies.between(start, end, size=1000, service=service, service_keys=repo.scenario.entity_keys)
    c = Ctx(repo, rca_config, service, route, start, end, found)
    pick_reference(c, need_traces=any(s in names for s in TRACE_SECTIONS))
    c.out = [f"# 根因分析：{c.what}，{_local(start, tz)}～{_local(end, tz)}（{tz}）", ""]
    for s in names:
        st.load(s)(c)     # 登記了的從 run 登記表拿；自己寫的段落：套件.模組:名字
    return "\n".join(c.out), c.findings

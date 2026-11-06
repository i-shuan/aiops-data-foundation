"""⑥⑧ aiops-alerts 的契約：index 名、mapping、@timestamp 往前寫的規則（stamp_of）、Alert（一件告警，寫進去的一筆）。
誰寫：investigator/cmd_alert（一件一筆、心跳）、detector/cmd_baseline（detect-status）、api/（標 rca_requested）；
誰讀：investigator/（下一輪併件）、api/（表單、清單）、feedback/（量化）、Grafana。寫的人和讀的人在不同階段，所以放 core/。

Alert 是 ⑥ 併件時在手上的那個物件：異常併進來（attach）、連帶上游（join_upstream、fold_into）、標題與摘要（headline、summary）都是它的方法，
incident/merge.py 只做「哪筆異常歸哪件」的演算法。同時是寫進 ES 的格式（to_doc）、讀回來的格式（from_doc）。
時間欄位在物件裡是 datetime（ES 的字串、"Z" 結尾都收），to_doc 轉 ISO 字串。這一輪算的、不寫 ES 的（anoms、new…）標 exclude。
心跳與 detect-status 不是一件事，照舊是兩個小 dict（heartbeat_doc、write_detect_status）。"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import ClassVar, Literal

from pydantic import Field, field_validator

from detect.store.anomaly import entity_of
from shared.document import Doc
from shared.clock import STAMP_LAG, stamp_of  # noqa: F401
from shared.incident import classify, describe_class

ALERT_INDEX = "aiops-alerts"
# stamp_of、STAMP_LAG 在 shared/clock.py（detect 的狀態心跳也用）；這裡 import 進來，舊的引用不用改


ALERT_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "alert_id": {"type": "keyword"}, "category": {"type": "keyword"},
    "service": {"type": "keyword"}, "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "severity": {"type": "keyword"}, "status": {"type": "keyword"},
    "started_at": {"type": "date"}, "updated_at": {"type": "date"}, "resolved_at": {"type": "date"},
    "anomaly_ids": {"type": "keyword"}, "anomaly_count": {"type": "integer"}, "series": {"type": "keyword"},
    "routes": {"type": "keyword"},
    # 跨服務（一層）：這件連帶了哪些上游服務；上游那件被整件併進下游時，上游那件標 resolved、merged_into 指到下游那件
    "upstream_services": {"type": "keyword"}, "merged_into": {"type": "keyword"},
    # 工廠版：這件牽涉哪些 lot（異常的 keys.lot；前後站併件時「同一批 lot」優先，investigator/alerts.merge 的 links）
    "lots": {"type": "keyword"},
    # 根因分析重跑：沒有 trace 的件（開件太快、trace 還沒合成完）每隔一陣子重跑，這是跑了幾次
    "rca_attempts": {"type": "integer"},
    # 同源（不合併）：這件卡在哪些外部依賴；跟它卡同一個依賴的其他開著的件
    "dependencies": {"type": "keyword"}, "related_alerts": {"type": "keyword"}, "shared_dependency": {"type": "keyword"},
    # Grafana 的告警只能帶 labels（terms 分組的值），所以標題、摘要、報告連結都存成 keyword
    "title": {"type": "keyword", "ignore_above": 1024}, "summary": {"type": "keyword", "ignore_above": 4096},
    "report_url": {"type": "keyword"}, "report": {"type": "text", "index": False}, "report_at": {"type": "date"},
    # 標題一行＋報告全文：Grafana 表格的 title 欄用它（值對應只顯示第一行），點那格的眼睛（Inspect value）就是全文
    "report_titled": {"type": "text", "index": False},
    "feedback_url": {"type": "keyword"},
    # 結構化的根因分析結果（相似事故檢索、LLM 用；不索引）、相似事故、LLM 摘要
    "findings": {"type": "object", "enabled": False}, "similar": {"type": "object", "enabled": False},
    "llm_summary": {"type": "text", "index": False}, "llm_provider": {"type": "keyword"},
    # case 只做分類（investigator/similar.classify）：像哪一類、信心；回饋表單預帶、Grafana 可依它分組
    "suggested_category": {"type": "keyword"}, "category_confidence": {"type": "double"},
    # 嚴重度怎麼來的（investigator/severity.py）：訊號廣度、持續、換版上限；deploy_at 是這個服務最近一次換版
    "severity_why": {"type": "text", "index": False}, "deploy_at": {"type": "date"},
    # detector detect 每輪寫的 detect-status（detector/cmd_baseline.py）：這輪沒有 baseline 的序列數；Grafana 的 aiops-baseline-missing 查它
    "missing_baseline": {"type": "integer"},
}}}
# 有寫進 _source、但沒進 mapping 的欄位（dynamic false：存得下、查不到）：api POST /api/rca 標的，下一輪 alert 看過就拿掉
UNMAPPED_FIELDS = ("rca_requested", "rca_requested_at")

AlertStatus = Literal["open", "resolved"]
SEVERITY = {"warning": 1, "high": 2, "critical": 3}   # 一件最後的嚴重度由 incident/severity.grade 算；這裡只是異常自己帶的
_LIST_FIELDS = ("anomaly_ids", "series", "routes", "lots", "upstream_services", "similar", "dependencies", "related_alerts", "shared_dependency")


class Alert(Doc):
    """aiops-alerts 的一件。欄位的意思見 docs/aiops-data-reference.md「aiops-alerts」。
    誰寫哪些：alerts.merge 開件、併件（alert_id、category、service、status、時間、anomaly_ids、series、routes、lots、upstream_services、severity）；
    cmd_alert 補歸屬（dept、group）、嚴重度（severity、severity_why、deploy_at）、根因分析（report*、findings、similar、suggested_category、
    category_confidence、llm_*、rca_attempts、report_at）、同源（dependencies、related_alerts、shared_dependency）、標題與摘要；api 標 rca_requested。"""
    timestamp: datetime | None = Field(default=None, alias="@timestamp")
    alert_id: str
    category: str
    service: str
    status: AlertStatus
    started_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    severity: str = "warning"
    dept: str | None = None
    group: str | None = None
    anomaly_ids: list[str] = []
    series: list[str] = []
    routes: list[str] = []
    lots: list[str] = []
    upstream_services: list[str] = []
    merged_into: str | None = None
    title: str | None = None
    summary: str | None = None
    report: str | None = None
    report_titled: str | None = None
    report_url: str | None = None
    feedback_url: str | None = None
    report_at: datetime | None = None
    rca_attempts: int | None = None
    rca_requested: bool | None = None
    rca_requested_at: datetime | None = None
    findings: dict | None = None
    similar: list[dict] = []
    suggested_category: str | None = None
    category_confidence: float | None = None
    llm_summary: str | None = None
    llm_provider: str | None = None
    severity_why: str | None = None
    deploy_at: datetime | None = None
    dependencies: list[str] = []
    related_alerts: list[str] = []
    shared_dependency: list[str] = []
    anomaly_count: int | None = None
    missing_baseline: int | None = None          # 只有 detect-status 那筆有；列在這裡是為了跟 mapping 對得上
    # 這一輪算的，不寫 ES（exclude）
    anoms: list[dict] = Field(default_factory=list, exclude=True)      # 這一輪併進來的異常（含 _id）
    new: bool = Field(default=False, exclude=True)                      # 這一輪新開的
    upgraded: bool = Field(default=False, exclude=True)                 # 這一輪嚴重度升級了
    rerun: bool = Field(default=False, exclude=True)                    # 這一輪重跑了根因分析
    report_text: str | None = Field(default=None, exclude=True)         # 這一輪的報告全文（寫 ES 前加回饋連結成 report）
    llm: dict | None = Field(default=None, exclude=True)                # 這一輪的 LLM 摘要（summarize 的回傳）

    MAX_IDS: ClassVar[int] = 500          # 一件最多記幾筆異常 id（很長的事故不要讓文件無限長）

    @field_validator(*_LIST_FIELDS, mode="before")
    @classmethod
    def _lists(cls, v):
        return [] if v is None else list(v)

    # ── 併件（incident/merge.py 的演算法呼叫）──
    @staticmethod
    def _ts(s) -> datetime:
        return s if isinstance(s, datetime) else datetime.fromisoformat(str(s).replace("Z", "+00:00"))

    @classmethod
    def open_for(cls, cat: str, name: str, first: dict) -> "Alert":
        """為第一筆異常新開一件：alert_id 是「a-時間-對象」（資料品質加 -dq）。"""
        t = cls._ts(first["@timestamp"])
        aid = f"a-{t:%Y%m%d-%H%M}-{re.sub(r'[^0-9A-Za-z_.-]+', '_', name)}" + ("-dq" if cat == "data_quality" else "")
        return cls(alert_id=aid, category=cat, service=name, status="open", started_at=t, updated_at=t, resolved_at=None,
                   anomaly_ids=[], series=[], routes=[], lots=[], upstream_services=[], severity="warning", anoms=[], new=True)

    def attach(self, a: dict, route: str | None) -> None:
        """一筆異常併進這件：時間範圍、anomaly_ids（最多 MAX_IDS）、序列、API（route 由情境算好傳進來）、lot、嚴重度（只升不降）。"""
        t = self._ts(a["@timestamp"])
        self.updated_at = max(self.updated_at, t)
        self.started_at = min(self.started_at, t)
        if len(self.anomaly_ids) < self.MAX_IDS:
            self.anomaly_ids.append(a["_id"])
        if a.get("series") and a["series"] not in self.series:
            self.series.append(a["series"])
        if route and route not in self.routes:
            self.routes.append(route)
        lot = (a.get("keys") or {}).get("lot")
        if lot is not None and str(lot) not in self.lots and len(self.lots) < self.MAX_IDS:
            self.lots.append(str(lot))
        sev = a.get("severity") or "warning"
        if SEVERITY.get(sev, 1) > SEVERITY.get(self.severity, 1):
            self.severity, self.upgraded = sev, not self.new
        self.anoms.append(a)

    def join_upstream(self, upstream: str) -> None:
        """記下「這件連帶了哪個上游服務」。"""
        if upstream not in self.upstream_services:
            self.upstream_services.append(upstream)

    def fold_into(self, down: "Alert", now: datetime) -> None:
        """這件（上游）整件併進下游那件：異常 id、序列、API、lot、嚴重度、這一輪的異常都搬過去。
        這件是上一輪已寫進 ES 的：標 resolved、merged_into（alert_service 照寫回去，但不再把它的異常標成它、不寫事故）。"""
        for i in self.anomaly_ids:
            if i not in down.anomaly_ids and len(down.anomaly_ids) < self.MAX_IDS:
                down.anomaly_ids.append(i)
        down.series += [s for s in self.series if s not in down.series]
        down.routes += [r for r in self.routes if r not in down.routes]
        down.lots += [l for l in self.lots if l not in down.lots][:max(0, self.MAX_IDS - len(down.lots))]
        if SEVERITY.get(self.severity, 1) > SEVERITY.get(down.severity, 1):
            down.severity = self.severity
        down.started_at = min(down.started_at, self.started_at)
        down.updated_at = max(down.updated_at, self.updated_at)
        down.anoms += self.anoms
        down.join_upstream(self.service)
        if not self.new:
            self.status, self.resolved_at, self.merged_into, self.anoms = "resolved", now, down.alert_id, []

    def close(self, at: datetime) -> None:
        self.status, self.resolved_at = "resolved", at

    # ── 這件自己的異常、歸屬、標題、摘要 ──
    def own_anomalies(self, entity_keys: tuple[str, ...]) -> list[dict]:
        """這一輪的異常裡，屬於這件自己服務的（連帶進來的上游不算）；一筆都沒有就全部。"""
        own = [a for a in self.anoms if entity_of(a, entity_keys) == (self.category, self.service)]
        return own or self.anoms

    def rca_route(self, route_of, entity_keys: tuple[str, ...]) -> str | None:
        """根因分析只查一支 API 的條件：這件自己服務的異常只有一支 API（route_of：情境算「哪支 API」的函式）。
        連帶進來的上游的 API 不算（下游沒有那支，查了會是空的）。"""
        if not self.upstream_services:
            return self.routes[0] if len(self.routes) == 1 else None
        own = {route_of(a.get("keys") or {}) for a in self.own_anomalies(entity_keys)} - {None}
        return next(iter(own)) if len(own) == 1 else None

    def owner(self, entity_keys: tuple[str, ...]) -> tuple[str | None, str]:
        """這件的 dept、group：取它的異常裡最多的（異常寫入時已補上）；連帶的上游不算（根因在下游，送下游的部門）。資料品質一律送平台。"""
        if self.category == "data_quality":
            return None, "unassigned"
        anoms = self.own_anomalies(entity_keys)
        dept = Counter(a["dept"] for a in anoms if a.get("dept")).most_common(1)
        group = Counter(a["group"] for a in anoms if a.get("group")).most_common(1)
        return (dept[0][0] if dept else self.dept), (group[0][0] if group else self.group or "unassigned")

    def headline(self, entity_keys: tuple[str, ...], count: bool = True) -> str:
        """一行標題：嚴重度、服務、最嚴重的那筆（自己服務的優先）、共幾項；連帶了上游就加在最後。"""
        top = max(self.own_anomalies(entity_keys), key=lambda a: (SEVERITY.get(a.get("severity") or "warning", 1), abs(a.get("z") or 0)), default=None)
        what = ""
        if top:
            msg = top.get("message")
            if msg:
                what = msg.split("：", 1)[0] if "：" in msg else msg
            else:
                med = f"（平常 {top['median']:.4g}）" if top.get("median") is not None else ""
                what = f"{top.get('series')} {top.get('value', 0):.4g}{med}"
        n = len(self.anomaly_ids)
        return (f"[{self.severity}] {self.service}：{what}" + (f" 等 {n} 項異常" if count and n > 1 else "")
                + (f"；連帶上游 {'、'.join(self.upstream_services)}" if self.upstream_services else ""))

    @staticmethod
    def conclusion(report: str | None) -> list[str]:
        """根因報告「結論」那一段的條列。"""
        m = re.search(r"^## 結論[^\n]*\n(.*)", report or "", re.S | re.M)
        return [l[2:].strip() for l in (m.group(1).splitlines() if m else []) if l.startswith("- ")]

    def make_summary(self, report: str | None, tz, similar: list[dict] | None, entity_keys: tuple[str, ...]) -> str:
        """通知用的摘要。Grafana 把它當 label（alert-rules.yaml 的 terms summary）：內容一變，Grafana 就當成另一則告警
        （舊的 resolved、新的 firing），所以不放每分鐘都會變的東西——「持續 N 分鐘」、「等 N 項異常」都不放
        （Grafana 自己會顯示從什麼時候開始；筆數在 title 和 anomaly_count）。只在根因結論、相似事故、連帶的上游加進來時才變。"""
        start = self.started_at.astimezone(tz)
        lines = [self.headline(entity_keys, count=False), f"從 {start:%m-%d %H:%M} 開始；異常：{'、'.join(self.series[:6])}"]
        concl = self.conclusion(report)
        if concl:
            lines.append("根因分析：" + "；".join(re.sub(r"\*\*", "", c) for c in concl[:3]))
        if self.shared_dependency and self.related_alerts:
            lines.append(f"可能同源：{'、'.join(self.related_alerts[:3])} 也卡在 {'、'.join(self.shared_dependency[:3])}")
        if similar:
            # case 只做分類：通知裡只說像哪一類、為什麼像，不給當時的處置（docs/pdca/meta/2026-11-case-classify.md）
            lines.append(f"分類：{describe_class(classify(similar))}；最像 {(similar[0].get('start') or '')[:10]}（{'；'.join(similar[0]['why'][:2])}）")
        return "\n".join(lines)

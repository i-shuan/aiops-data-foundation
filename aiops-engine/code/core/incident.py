"""⑦⑨ 事故（aiops-incidents）與它的指紋：**欄位只定義在這裡**（docs/pdca/meta/2026-11-vo-models.md）。

寫 aiops-incidents 的四條路（事件整合自動開：investigator/cmd_alert；歷史事故單匯入：investigator/importer；人工補登：
detector/cmd_feedback cmd_incident；回饋網頁 upsert 的種子：api/app）以前各自手寫一份 dict，欄位名打錯要到讀的時候才知道。
現在都建 Incident 再 to_doc()；讀回來用 Incident.from_doc()（舊文件缺欄位給預設值、多出來的鍵忽略，不丟例外）。

指紋（Fingerprint）是相似事故檢索的比對依據（investigator/similar.py）：六組 keyword 陣列。從告警＋根因分析算（from_alert）、
從匯入的一列算（normalized）都走同一套正規化：段名去數字、慢的段與錯誤源頭最多 3 個、log 樣式截 120 字最多 5 個。

跟 detector/base.py 的 Anomaly 同一個做法：模型是唯一定義處，INCIDENT_MAPPING 要跟它一致（tests/test_incident.py 會對）。
時間欄位目前都是 ISO 字串（三條寫入路徑拿到的本來就是字串），這一輪不轉 datetime。
"""
from __future__ import annotations

import re
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

INCIDENT_INDEX = "aiops-incidents"

# status：open／resolved（人補登的）、auto（事件整合自動開的，還沒人看）、confirmed（老師傅標真的）、corrected（真的但根因改過）、
# false_positive（老師傅說誤報）、merged（上游那件整件併進下游，不算一件）。相似事故檢索只用 confirmed、corrected（investigator/similar.py）。
IncidentStatus = Literal["open", "resolved", "auto", "confirmed", "corrected", "false_positive", "merged"]

INCIDENT_MAPPING = {"mappings": {"dynamic": False, "properties": {
    "@timestamp": {"type": "date"}, "title": {"type": "text"}, "start": {"type": "date"}, "end": {"type": "date"},
    "services": {"type": "keyword"}, "severity": {"type": "keyword"}, "status": {"type": "keyword"},
    "root_cause": {"type": "text"}, "action": {"type": "text"}, "anomaly_ids": {"type": "keyword"},
    "detected_at": {"type": "date"}, "resolved_at": {"type": "date"}, "by": {"type": "keyword"},
    # 老師傅的經驗（docs/pdca/phase5-learning-loop/2026-10-expert-feedback.md）
    "alert_id": {"type": "keyword"}, "dept": {"type": "keyword"}, "group": {"type": "keyword"},
    "root_cause_category": {"type": "keyword"}, "how_detected": {"type": "text"}, "checked": {"type": "keyword"},
    "improve": {"type": "text"}, "confirmed_at": {"type": "date"}, "confirmed_by": {"type": "keyword"},
    # 歷史事故單匯入（investigator/importer.py）：source import；note 是匯入檔的備註
    "source": {"type": "keyword"}, "note": {"type": "text"},
    # 指紋：相似事故檢索用（investigator/similar.py）
    "fingerprint": {"properties": {"services": {"type": "keyword"}, "routes": {"type": "keyword"}, "series": {"type": "keyword"},
                                   "slow_segments": {"type": "keyword"}, "error_origins": {"type": "keyword"},
                                   "patterns": {"type": "text"}}}}}}
# 有寫進 _source、但沒進 mapping 的欄位（dynamic false：存得下、查不到）。要查就得加進 mapping 並重建索引，這一輪不動。
UNMAPPED_FIELDS = ("upstream_services", "merged_into")


def seg_name(segment: str | None) -> str:
    """段的名字去掉數字（SQL 裡的 id、批號），同一種呼叫才對得起來。"""
    return re.sub(r"\d+", "#", segment or "").strip()


def _as_list(v) -> list[str]:
    """ES 回來的 keyword 欄位可能是一個值或一串；None 當空；元素裡的 None 丟掉（[alert.get("service")] 可能是 [None]）。"""
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v if x is not None]
    return [str(v)]


class Doc(BaseModel):
    """寫進 ES 的文件共同的做法：建構時多出來的鍵就是錯（打錯欄位名立刻擋）；to_doc 不寫 None；from_doc 讀舊文件寬鬆。"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)

    @classmethod
    def from_doc(cls, d: dict | None):
        """ES 的 _source → 物件。不認得的鍵忽略（舊文件、別的程式多寫的），缺的給預設值。"""
        d = d or {}
        names = {f.alias or n for n, f in cls.model_fields.items()} | set(cls.model_fields)
        return cls.model_validate({k: v for k, v in d.items() if k in names})

    def to_doc(self) -> dict:
        """寫進 ES 的 dict：欄位名用 ES 的（@timestamp）；只寫建構時有給的欄位（沒碰的預設值不寫，匯入的事故不會多出 anomaly_ids: []），
        給了但是 None 的也不寫；給了空陣列照寫。from_doc 讀進來再 to_doc 會跟原文件一樣。"""
        return self.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)


class Fingerprint(Doc):
    """一件事的指紋：全部是 keyword 陣列，存進 aiops-incidents.fingerprint，也拿來查。欄位名就是比對的特徵名（similar.WEIGHTS 的鍵）。"""
    services: list[str] = []
    routes: list[str] = []
    series: list[str] = []
    slow_segments: list[str] = []
    error_origins: list[str] = []
    patterns: list[str] = []

    FIELDS: ClassVar[tuple[str, ...]] = ("services", "routes", "series", "slow_segments", "error_origins", "patterns")
    MAX_SEGMENTS: ClassVar[int] = 3
    MAX_PATTERNS: ClassVar[int] = 5
    PATTERN_LEN: ClassVar[int] = 120

    @field_validator(*FIELDS, mode="before")
    @classmethod
    def _lists(cls, v):
        return _as_list(v)

    def to_doc(self) -> dict:
        """指紋六組一定都寫（沒有的是 []），查詢與比對才不用分「沒有這個欄位」和「空」。"""
        return self.model_dump()

    @property
    def empty(self) -> bool:
        """六組都空：沒東西可比，檢索直接回空（不然 minimum_should_match 1 會查到任何一件）。"""
        return not any(getattr(self, f) for f in self.FIELDS)

    def get(self, field: str) -> list[str]:
        return list(getattr(self, field))

    @classmethod
    def of(cls, x: "Fingerprint | dict | None") -> "Fingerprint":
        """dict（ES 讀回來的、舊程式傳進來的）或已經是 Fingerprint 都收；None 是空指紋。"""
        if isinstance(x, Fingerprint):
            return x
        return cls.from_doc(x)

    @classmethod
    def normalized(cls, services=(), routes=(), series=(), slow_segments=(), error_origins=(), patterns=()) -> "Fingerprint":
        """正規化的唯一一處：服務、API、指標去重排序；段名去數字、最多 3 個；log 樣式截 120 字、最多 5 個。
        from_alert（自動算的）和匯入（人填的）都經這裡，兩邊才對得起來。"""
        return cls(services=sorted({s for s in _as_list(services) if s}),
                   routes=sorted(set(_as_list(routes))), series=sorted(set(_as_list(series))),
                   slow_segments=[seg_name(s) for s in _as_list(slow_segments)][:cls.MAX_SEGMENTS],
                   error_origins=[seg_name(s) for s in _as_list(error_origins)][:cls.MAX_SEGMENTS],
                   patterns=[p[:cls.PATTERN_LEN] for p in _as_list(patterns)][:cls.MAX_PATTERNS])

    @classmethod
    def from_alert(cls, alert: dict, findings: dict | None) -> "Fingerprint":
        """告警（aiops-alerts 的 service、routes、series）＋根因分析的結果（segments、error_origins、new_patterns）→ 指紋。
        服務：告警的服務＋最慢三段的第一個服務；API、指標：告警的，沒有才用根因分析的；慢的段只算真的變慢的（delta_ms > 0）。"""
        f = findings or {}
        services = [alert.get("service")] if alert.get("service") else []
        services += [s.split(" › ")[0] for s in (d["segment"] for d in f.get("segments", [])[:cls.MAX_SEGMENTS])]
        return cls.normalized(
            services=services,
            routes=alert.get("routes") or ([f["route"]] if f.get("route") else []),
            series=alert.get("series") or [a["series"] for a in f.get("anomalies", []) if a.get("series")],
            slow_segments=[d["segment"] for d in f.get("segments", []) if d.get("delta_ms", 0) > 0],
            error_origins=[d["segment"] for d in f.get("error_origins", [])],
            patterns=[p["pattern"] for p in f.get("new_patterns", []) if p.get("is_new")])


_TEXT_FIELDS = ("alert_id", "title", "start", "end", "severity", "source", "dept", "group", "detected_at", "resolved_at",
                "root_cause_category", "root_cause", "action", "how_detected", "improve", "by", "confirmed_at", "confirmed_by",
                "note", "merged_into")
_LIST_FIELDS = ("services", "anomaly_ids", "upstream_services", "checked")


class Incident(Doc):
    """aiops-incidents 的一筆。誰寫哪些欄位：
      事件整合自動開（status auto）  @timestamp、alert_id、title、start、end、services（只有這件自己的服務）、severity、upstream_services、
                                      dept、group、anomaly_ids（最多 200）、detected_at、fingerprint；併進下游時多 status merged、merged_into
      匯入（status confirmed）        title、start、end、services、source import、detected_at／resolved_at（= start／end）、root_cause_category、
                                      root_cause、action、how_detected、checked、fingerprint、by（同時寫 confirmed_by）、note、alert_id imp-…
      人工補登（status open）         title、start、end、services、severity、root_cause、action、by、anomaly_ids
      回饋網頁                        upsert：種子是告警的 title／start／end／services／severity／dept／group／anomaly_ids ＋ 老師傅填的
                                      status、confirmed_at、confirmed_by、by、root_cause_category、root_cause、action、how_detected、checked、improve
    時間欄位都是 ISO 字串。@timestamp 四條寫入路徑都會給；型別上不設必填，是因為 from_doc 要讀得了測試的假文件與別人手寫的舊文件。"""
    timestamp: str | None = Field(default=None, alias="@timestamp")
    status: IncidentStatus
    alert_id: str | None = None
    title: str | None = None
    start: str | None = None
    end: str | None = None
    services: list[str] = []
    severity: str | None = None
    source: str | None = None
    dept: str | None = None
    group: str | None = None
    anomaly_ids: list[str] = []
    upstream_services: list[str] = []
    detected_at: str | None = None
    resolved_at: str | None = None
    root_cause_category: str | None = None
    root_cause: str | None = None
    action: str | None = None
    how_detected: str | None = None
    checked: list[str] = []
    improve: str | None = None
    by: str | None = None
    confirmed_at: str | None = None
    confirmed_by: str | None = None
    note: str | None = None
    merged_into: str | None = None
    fingerprint: Fingerprint | None = None

    MAX_ANOMALY_IDS: ClassVar[int] = 200

    @field_validator(*_LIST_FIELDS, mode="before")
    @classmethod
    def _lists(cls, v):
        return _as_list(v)

    @field_validator(*_TEXT_FIELDS, mode="before")
    @classmethod
    def _text(cls, v):
        """空字串當沒填（人工補登、表單送來的空格），to_doc 就不寫。數字（舊資料的 severity 之類）轉成字串。"""
        if v is None:
            return None
        s = str(v).strip() if not isinstance(v, str) else v.strip()
        return s or None

    @field_validator("fingerprint", mode="before")
    @classmethod
    def _fp(cls, v: Any):
        if v is None or isinstance(v, Fingerprint):
            return v
        return Fingerprint.of(v) if isinstance(v, dict) else None

    @field_serializer("fingerprint")
    def _fp_doc(self, v: Fingerprint | None):
        return v.to_doc() if v is not None else None

    @classmethod
    def from_alert(cls, al: dict, doc: dict, findings: dict | None, **override) -> "Incident":
        """事件整合自動開的事故（status auto）：回饋表單確認後變 confirmed，才會被相似事故檢索拿來用。
        al 是這一輪算好的一件（investigator/alerts.py）、doc 是要寫進 aiops-alerts 的那份（時間已是字串）。
        services 只放這件自己的服務（根因在下游；feedback 的規則建議依 services 寫 match）；連帶的上游另外記 upstream_services。
        override：併進下游時給 status="merged", merged_into=…。"""
        return cls(**{"@timestamp": doc["@timestamp"], "alert_id": al["alert_id"], "title": doc.get("title"), "start": doc["started_at"],
                      "end": doc.get("resolved_at") or doc["updated_at"], "services": [al["service"]], "severity": al["severity"],
                      "upstream_services": list(al.get("upstream_services") or []), "status": "auto",
                      "dept": doc.get("dept"), "group": doc.get("group"), "anomaly_ids": al["anomaly_ids"][:cls.MAX_ANOMALY_IDS],
                      "detected_at": doc["started_at"], "fingerprint": Fingerprint.from_alert(al, findings), **override})

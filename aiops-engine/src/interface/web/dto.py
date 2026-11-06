"""HTTP 層的資料形狀：告警 → 表單預填（form_data、form_json）、JSON 的輸出（to_json）、Grafana 表單元素的 id、這個情境的 ingest.yaml 在哪。
不碰 ES（service 做）、不產 HTML（views 做）。"""
from __future__ import annotations

import json
import os

from shared.env import CODE_ROOT
from learn.store.feedback import RCA_USEFUL as _RCA_USEFUL
from shared.incident_doc import classify
from interface.jobs.runner import load_schedule
from shared.scenario import Scenario
from learn.feedback_service import RCA_QUESTIONS as _RCA_QUESTIONS, SERIES_NAMES, TIMEZONE, WEB_VERDICTS  # noqa: F401  給 views、測試用

MISSED_FIELDS = ("series", "service", "at", "by", "note", "route", "pod")    # 漏報表單的元素 id（Grafana 預填的 key 要對到這些）
VERDICTS = dict(zip(WEB_VERDICTS, ("真的", "誤報", "真的，但根因報錯了")))     # 表單的選項與顯示文字
RCA_USEFUL = list(zip(_RCA_USEFUL, ("是", "否", "沒看")))                        # 採納率的兩題：是／否／沒看；沒填不算
RCA_QUESTIONS = list(zip(_RCA_QUESTIONS, ("可能根因有參考價值？", "建議的排查動作有參考價值？")))


def form_data(alert: dict) -> dict:
    """給表單用的預填資料。"""
    sim = alert.get("similar") or []
    cat = alert.get("suggested_category")
    if cat is None and sim:                      # 舊的告警（分類器之前寫的）：現場算
        cls = classify(sim)
        cat = cls["category"] if cls else None
    return {"alert_id": alert["alert_id"], "title": alert.get("title"), "summary": alert.get("summary") or "", "service": alert.get("service"),
            "dept": alert.get("dept"), "severity": alert.get("severity"), "status": alert.get("status"), "started_at": alert.get("started_at"),
            "llm": alert.get("llm_summary") or "", "similar": sim[:3], "anomaly_ids": alert.get("anomaly_ids") or [],
            "series": alert.get("series") or [], "report_url": alert.get("report_url"), "report": alert.get("report") or "",
            "suggested_category": cat, "category_confidence": alert.get("category_confidence")}


def sim_line(s: dict) -> str:
    """相似 case 一行：日期 [分類] 標題｜為什麼像。不給當時的處置（case 只做分類，docs/pdca/meta/2026-11-case-classify.md）。"""
    return (f"{(s.get('start') or '')[:16].replace('T', ' ')} [{s.get('root_cause_category') or '—'}] {s.get('title') or ''}"
            f"｜為什麼像：{'；'.join(s.get('why') or [])}")


def form_json(d: dict) -> dict:
    """給 Grafana 表單預填用：一層的 key → 字串（表單元素的 id 對到這些 key）。相似事故攤成幾行文字；
    root_cause_category 預帶分類器的結果（表單那格的 id 就是它），人改掉就是 corrected。"""
    sim = "\n".join(sim_line(s) for s in d["similar"]) or "沒有相似的過去事故"
    return {"alert_id": d["alert_id"], "title": d["title"] or d["alert_id"], "summary": d["summary"], "llm": d["llm"] or "（沒有 AI 摘要）",
            "similar": sim, "service": d["service"], "dept": d["dept"], "report_url": d["report_url"] or "",
            "root_cause_category": d.get("suggested_category") or ""}


def missed_form_json() -> dict:
    """漏報表單的預填（initial request）：沒有東西可預填，回空字串讓外掛把欄位清掉。"""
    return {k: "" for k in MISSED_FIELDS}


def to_json(x) -> bytes:
    return json.dumps(x, ensure_ascii=False, default=str).encode("utf-8")


def ingest_config_path(sc: Scenario) -> str | None:
    """POST /api/anomalies 用哪份 ingest.yaml：AIOPS_INGEST_CONFIG，沒設就從這個情境的排程表找 ingest 工作的 --config。
    排程表沒有 ingest（observability 自己判，沒有外部來源）回 None。"""
    if os.environ.get("AIOPS_INGEST_CONFIG"):
        return os.environ["AIOPS_INGEST_CONFIG"]
    _, jobs = load_schedule(str(CODE_ROOT / sc.schedule), stage=2)
    job = next((j for j in jobs if j.name == "ingest"), None)
    if job is None or "--config" not in job.args:
        return None
    return str(CODE_ROOT / job.args[job.args.index("--config") + 1])

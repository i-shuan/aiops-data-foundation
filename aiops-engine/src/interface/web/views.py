"""HTML 頁面（Grafana 的 Business Forms 外掛裝不起來時的備用網頁）：模板在 templates/*.html（jinja2，自動跳脫），這裡只組模板要的資料。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader

from interface.web.dto import RCA_QUESTIONS, RCA_USEFUL, SERIES_NAMES, TIMEZONE, VERDICTS
from common.scenario import Scenario, load_scenario
import data.mapping_service as MAP
from common.scenario.mapping import EDITABLE, MappingRepository, profiles
from interface.config.schedule import trace_raw_pairs

TEXT_HINT = "選填。一句話說這件怎麼回事，系統會抽成結構化欄位、提一筆待審核的調整建議；例：「這是每月初的批次匯出報表，屬於預期內負載」"
_env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent / "templates")), autoescape=True, trim_blocks=True, lstrip_blocks=True)


def render_form(d: dict, done: dict | None = None, error: str | None = None, full: bool = False, notice: str | None = None,
                sc: Scenario | None = None) -> str:
    """一件的回饋表單。full：根因分析全文直接放在表單上面（從 Grafana 的連結點進來：先看全文、最下面填回饋，不用另開一頁）。
    notice：重跑根因分析之後的提示（POST /rca 轉回來時帶）。開著的件最上面有「重跑根因分析」。sc：分類表、常查的地方、誤報原因。"""
    sc = sc or load_scenario()
    extra = ""
    if done and done.get("extract"):
        x = done["extract"]
        extra = (f"已依你的一句話提了一筆調整建議（待審核，id {done['tuning_id']}）。" if done.get("tuning_id")
                 else f"一句話抽成：{x.get('feedback_type', '')}／{x.get('identified_cause', '')}／{x.get('target_service', '')}／{x.get('suggested_action', '')}。" if x.get("ok")
                 else "一句話沒抽成結構化欄位（只存了原文）。")
    done_text = f"已記錄：{VERDICTS.get(done['incident'], done['incident'])}（{done['feedback']} 筆回饋，{done['by']}）。謝謝，這會讓下一次更準。{extra}" if done else None
    return _env.get_template("feedback.html").render(
        d=d, done=done_text, error=error, full=full, notice=notice, verdicts=VERDICTS, rca_questions=RCA_QUESTIONS, rca_useful=RCA_USEFUL,
        categories=sc.categories, fp_reasons=sc.fp_reasons, checked=sc.checked, text_hint=TEXT_HINT, is_open=d.get("status") == "open")


def render_missed(done: dict | None = None, error: str | None = None, form: dict | None = None) -> str:
    """漏報（該報卻沒報）的網頁版：欄位跟 Grafana 外掛的漏報面板一樣（dto.MISSED_FIELDS），送出走 feedback_service.submit_missed。
    一般文字欄、預填現在（台北）：datetime-local 在舊瀏覧器、內嵌在 Grafana 的 iframe 裡不好填；submit_missed 本來就吃這個格式。"""
    f = {k: str(v or "") for k, v in (form or {}).items()}
    return _env.get_template("missed.html").render(done=done, error=error, f=f, series_names=SERIES_NAMES,
                                                    now_local=pd.Timestamp.now(tz=TIMEZONE).strftime("%Y-%m-%d %H:%M"))


def render_report(alert: dict) -> str:
    return _env.get_template("report.html").render(alert=alert)


def render_mapping(es, sc: Scenario, done: dict | None = None, error: str | None = None, form: dict | None = None, tried=None) -> str:
    """欄位對照的網頁：現在生效的值（標出被覆寫的）、提一筆的表單（可以先試）、提過的清單。核准不在這裡（命令列）。"""
    try:
        rows, history = MAP.current(es, sc, trace_raw_pairs(sc.schedule)), MappingRepository(es).all()[:50]
    except Exception as e:  # noqa: BLE001  ES 連不上：頁面照樣出來，說清楚
        rows, history, error = [], [], error or f"讀不到 ES（{type(e).__name__}：{str(e)[:120]}）"
    return _env.get_template("mapping.html").render(rows=rows, history=history, done=done, error=error, tried=tried,
                                                    f={k: str(v or "") for k, v in (form or {}).items()}, sources=list(profiles(sc)), editable=EDITABLE)


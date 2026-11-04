"""回饋表單的邏輯（不含 HTTP）：讀告警、組表單資料、寫回饋與事故。單元測試用假的 ES 測這裡。"""
from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from core import domain
from core import llm as LLM
from core.es import doc_id
from detector import tuning as T
from detector.feedback import FEEDBACK_MAPPING, INCIDENT_MAPPING, RCA_USEFUL as _RCA_USEFUL

ALERT_INDEX, FEEDBACK_INDEX, INCIDENT_INDEX = "aiops-alerts", "aiops-feedback", "aiops-incidents"
_ready = set()
# 漏報表單的序列選單、時區：跟 detector 讀同一份 series.yaml（image 裡 configs/ 跟套件放在一起，路徑一樣）
_SERIES_CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "configs/baseline/series.yaml").read_text(encoding="utf-8"))
SERIES_NAMES = [s["name"] for s in _SERIES_CFG["series"]]
TIMEZONE = _SERIES_CFG.get("train", {}).get("timezone", "Asia/Taipei")
MISSED_FIELDS = ("series", "service", "at", "by", "note", "route", "pod")    # 漏報表單的元素 id（Grafana 預填的 key 要對到這些）
VERDICTS = {"true_positive": "真的", "false_positive": "誤報", "wrong_root_cause": "真的，但根因報錯了"}
# 採納率的兩題（docs/pdca/phase5-learning-loop/2026-10-feedback-nl-extract.md）：是／否／沒看；沒填不算
RCA_USEFUL = [(v, t) for v, t in zip(_RCA_USEFUL, ("是", "否", "沒看"))]
RCA_QUESTIONS = [("rca_cause_useful", "可能根因有參考價值？"), ("rca_action_useful", "建議的排查動作有參考價值？")]
TEXT_HINT = "選填。一句話說這件怎麼回事，系統會抽成結構化欄位、提一筆待審核的調整建議；例：「這是每月初的批次匯出報表，屬於預期內負載」"
# 根因分類、常查的地方、誤報原因依領域（AIOPS_DOMAIN：it／factory；core/domain.py）。Grafana 的表單（aiops-feedback.json）是 IT 版的選項
CATEGORIES = domain.categories()
CHECKED = domain.checked()
FP_REASONS = domain.fp_reasons()


def load_alert(es, alert_id: str) -> dict | None:
    got = es.req("GET", f"/{ALERT_INDEX}/_doc/{alert_id}", ok=(200, 404))
    return got.get("_source") if got.get("found") else None


def form_data(alert: dict) -> dict:
    """給表單用的預填資料。"""
    sim = alert.get("similar") or []
    cat = alert.get("suggested_category")
    if cat is None and sim:                      # 舊的告警（分類器之前寫的）：現場算
        from investigator.similar import classify
        cls = classify(sim)
        cat = cls["category"] if cls else None
    return {"alert_id": alert["alert_id"], "title": alert.get("title"), "summary": alert.get("summary") or "", "service": alert.get("service"),
            "dept": alert.get("dept"), "severity": alert.get("severity"), "status": alert.get("status"), "started_at": alert.get("started_at"),
            "llm": alert.get("llm_summary") or "", "similar": sim[:3], "anomaly_ids": alert.get("anomaly_ids") or [],
            "series": alert.get("series") or [], "report_url": alert.get("report_url"), "report": alert.get("report") or "",
            "suggested_category": cat, "category_confidence": alert.get("category_confidence")}


def _sim_line(s: dict) -> str:
    """相似 case 一行：日期 [分類] 標題｜為什麼像。不給當時的處置（case 只做分類，docs/pdca/meta/2026-11-case-classify.md）。"""
    return (f"{(s.get('start') or '')[:16].replace('T', ' ')} [{s.get('root_cause_category') or '—'}] {s.get('title') or ''}"
            f"｜為什麼像：{'；'.join(s.get('why') or [])}")


def form_json(d: dict) -> dict:
    """給 Grafana 表單預填用：一層的 key → 字串（表單元素的 id 對到這些 key）。相似事故攤成幾行文字；
    root_cause_category 預帶分類器的結果（表單那格的 id 就是它），人改掉就是 corrected。"""
    sim = "\n".join(_sim_line(s) for s in d["similar"]) or "沒有相似的過去事故"
    return {"alert_id": d["alert_id"], "title": d["title"] or d["alert_id"], "summary": d["summary"], "llm": d["llm"] or "（沒有 AI 摘要）",
            "similar": sim, "service": d["service"], "dept": d["dept"], "report_url": d["report_url"] or "",
            "root_cause_category": d.get("suggested_category") or ""}


def submit(es, form: dict, now: datetime | None = None) -> dict:
    """form：verdict、by、root_cause_category、root_cause、action、how_detected、checked（list）、improve、fp_reason、note、
    feedback_text（一句自然語言，選填）、rca_cause_useful、rca_action_useful（yes／no／not_seen，選填）。
    回傳 {"feedback": 寫了幾筆, "incident": 狀態, "by"}；有給 feedback_text 時多 "extract"（抽出來的四欄＋ok、provider、reason）
    和 "tuning_id"（寫進 aiops-tuning 的建議 id；suggested_action 是 none、或沒抽到就是 None）。
    抽取失敗不影響寫入：只存原文（extract.ok false）；寫建議失敗也不影響回饋本身（tuning_error）。"""
    now = now or datetime.now(timezone.utc)
    alert = load_alert(es, form["alert_id"])
    if not alert:
        raise KeyError(f"找不到告警 {form['alert_id']}")
    verdict = form["verdict"]
    if verdict not in VERDICTS:
        raise ValueError(f"verdict 要是 {'、'.join(VERDICTS)}")
    by = (form.get("by") or "").strip() or "anonymous"
    if id(es) not in _ready:    # index 要先用正式的 mapping 建（不然第一筆寫進去 ES 會自己猜型別，之後 feedback suggest 的 mapping 就合不上）
        es.ensure_index(FEEDBACK_INDEX, FEEDBACK_MAPPING)
        es.ensure_index(INCIDENT_INDEX, INCIDENT_MAPPING)
        _ready.add(id(es))
    base = {"@timestamp": now.isoformat(), "verdict": verdict, "by": by, "alert_id": alert["alert_id"],
            "alert_started_at": alert.get("started_at"), "incident_id": alert["alert_id"],
            "note": (form.get("note") or form.get("fp_reason_text") or "").strip() or None,
            "fp_reason": form.get("fp_reason") if verdict == "false_positive" else None,
            "correct_root_cause": form.get("root_cause") if verdict == "wrong_root_cause" else None}
    for q, _ in RCA_QUESTIONS:
        if form.get(q) in _RCA_USEFUL:
            base[q] = form[q]
    # 一句自然語言 → LLM 抽成四欄（core.llm.extract_feedback）；驗不過就只存原文。對得到調整種類就先寫一筆 proposed 的 aiops-tuning
    text, ext, tuning_id, tuning_error = (form.get("feedback_text") or "").strip(), None, None, None
    if text:
        ext = LLM.extract_feedback(text, alert)
        base["feedback_text"] = text
        base["extract"] = {k: v for k, v in {**(ext["fields"] or {}), "ok": ext["ok"], "provider": ext["provider"], "reason": ext["reason"]}.items() if v not in (None, "")}
        if ext["ok"]:
            try:
                tuning_id = T.propose_from_text(es, ext["fields"], alert, text, by, now)
            except Exception as e:    # noqa: BLE001  建議寫不進去（ES 權限、mapping）不能讓回饋本身失敗
                tuning_error = f"{type(e).__name__}：{str(e)[:120]}"
                sys.stderr.write(f"feedback tuning proposal failed: {e!r}\n")
            base["tuning_id"] = tuning_id
    base = {k: v for k, v in base.items() if v not in (None, "")}
    # 一件的每筆異常各一筆回饋：現有的 feedback summary／suggest 是依異常（series＋對象）算的，才對得起來
    ids = alert.get("anomaly_ids") or []
    got = es.req("POST", "/aiops-anomalies/_mget?_source_includes=@timestamp,series,keys", {"ids": ids[:200]}).get("docs", []) if ids else []
    docs = []
    for g in got:
        if not g.get("found"):
            continue
        src = g["_source"]
        d = {**base, "anomaly_id": g["_id"], "anomaly_at": src.get("@timestamp"), "series": src.get("series"), "keys": src.get("keys") or {}}
        docs.append((doc_id("feedback", g["_id"], by), d))
    if not docs:    # 異常已經不在了（過期）：至少留一筆對這件的
        docs.append((doc_id("feedback", alert["alert_id"], by), {**base, "series": (alert.get("series") or ["?"])[0], "keys": {"service": alert.get("service")}}))
    es.bulk_index(FEEDBACK_INDEX, docs)
    # 事故：確認 / 更正 / 誤報
    status = {"true_positive": "confirmed", "wrong_root_cause": "corrected", "false_positive": "false_positive"}[verdict]
    inc = {"status": status, "confirmed_at": now.isoformat(), "confirmed_by": by, "by": by}
    if verdict != "false_positive":
        inc.update({"root_cause_category": form.get("root_cause_category") or None, "root_cause": (form.get("root_cause") or "").strip() or None,
                    "action": (form.get("action") or "").strip() or None, "how_detected": (form.get("how_detected") or "").strip() or None,
                    "checked": [c for c in (form.get("checked") or []) if c] or None, "improve": (form.get("improve") or "").strip() or None})
    inc = {k: v for k, v in inc.items() if v is not None}
    # 事故可能還沒被 alert 開出來（例如回饋得很快）：用 upsert 補一筆最小的
    seed = {"@timestamp": now.isoformat(), "alert_id": alert["alert_id"], "title": alert.get("title"), "start": alert.get("started_at"),
            "end": alert.get("resolved_at") or alert.get("updated_at"), "services": [alert.get("service")], "severity": alert.get("severity"),
            "dept": alert.get("dept"), "group": alert.get("group"), "anomaly_ids": ids[:200], **inc}
    es.req("POST", f"/{INCIDENT_INDEX}/_update/{alert['alert_id']}?refresh=true", {"doc": inc, "upsert": seed})
    # 不再另外 _refresh：bulk_index 已經 refresh=wait_for；最小權限的 key 沒有 maintenance，多打這一下會 403，資料存好了卻回「沒有存成功」
    out = {"feedback": len(docs), "incident": status, "by": by}
    if text:
        out.update({"extract": base.get("extract"), "tuning_id": tuning_id, **({"tuning_error": tuning_error} if tuning_error else {})})
    return out


def missed_form_json() -> dict:
    """漏報表單的預填（initial request）：沒有東西可預填，回空字串讓外掛把欄位清掉。"""
    return {k: "" for k in MISSED_FIELDS}


def submit_missed(es, form: dict, now: datetime | None = None) -> dict:
    """漏報（該報卻沒報）：沒有 alert_id、沒有 anomaly_id，寫一筆 verdict: missed。
    form：series（series.yaml 裡的名字）、service、at（ISO 或 YYYY-MM-DD HH:MM；沒時區當 Asia/Taipei）、by、note、route／pod（選填，進 keys）。
    文件格式、_id 跟指令 feedback add --verdict missed 一樣（cmd_feedback.py），同一個人對同一條再送會覆蓋、不重複。"""
    now = now or datetime.now(timezone.utc)
    series = (form.get("series") or "").strip()
    if series not in SERIES_NAMES:
        raise ValueError(f"series 要是 {'、'.join(SERIES_NAMES)}")
    service = (form.get("service") or "").strip()
    if not service:
        raise ValueError("service 要填（哪個服務該報卻沒報）")
    raw_at = (form.get("at") or "").strip()
    if not raw_at:
        raise ValueError("at 要填（什麼時候該報：2026-10-25 10:00）")
    try:
        at = pd.Timestamp(raw_at)
    except (ValueError, TypeError) as e:
        raise ValueError(f"at 看不懂（{raw_at}）：寫 2026-10-25 10:00 或 ISO 格式") from e
    if pd.isna(at):
        raise ValueError("at 要填（什麼時候該報：2026-10-25 10:00）")
    by = (form.get("by") or "").strip() or "anonymous"
    keys = {"service": service, **{k: (form.get(k) or "").strip() for k in ("route", "pod") if (form.get(k) or "").strip()}}
    doc = {"@timestamp": now.isoformat(), "verdict": "missed", "series": series, "keys": keys,
           "anomaly_at": (at.tz_localize(TIMEZONE) if at.tzinfo is None else at).isoformat(), "by": by,
           "note": (form.get("note") or "").strip() or None}
    doc = {k: v for k, v in doc.items() if v not in (None, "")}
    if id(es) not in _ready:
        es.ensure_index(FEEDBACK_INDEX, FEEDBACK_MAPPING)
        es.ensure_index(INCIDENT_INDEX, INCIDENT_MAPPING)
        _ready.add(id(es))
    _id = doc_id("feedback", [doc["series"], doc["keys"], doc["anomaly_at"]], by)
    es.req("PUT", f"/{FEEDBACK_INDEX}/_doc/{_id}?refresh=true", doc)
    return {"feedback": 1, "verdict": "missed", "series": series, "service": service, "by": by}


def _opt(options, selected=None):
    return "".join(f'<option value="{html.escape(v)}"{" selected" if v == selected else ""}>{html.escape(t)}</option>' for v, t in options)


def render_form(d: dict, done: dict | None = None, error: str | None = None, full: bool = False) -> str:
    """full：根因分析全文直接放在表單上面（Grafana 抽屜裡用：先看全文、最下面填回饋，不用另開一頁）。"""
    e = html.escape
    sim = "".join(f"<li><b>{e((s.get('start') or '')[:16])}</b> [{e(s.get('root_cause_category') or '—')}] {e(s.get('title') or '')}"
                  f"<br><small>為什麼像：{e('；'.join(s.get('why') or []))}</small></li>"
                  for s in d["similar"]) or "<li>沒有相似的過去事故</li>"
    cat_hint = (f'<span class="hint">（系統依過去相似的 case 預帶 {e(d["suggested_category"])}，不對請改）</span>' if d.get("suggested_category") else "")
    extra = ""
    if done and done.get("extract"):
        x = done["extract"]
        extra = (f"已依你的一句話提了一筆調整建議（待審核，id {e(done['tuning_id'])}）。" if done.get("tuning_id")
                 else f"一句話抽成：{e(x.get('feedback_type', ''))}／{e(x.get('identified_cause', ''))}／{e(x.get('target_service', ''))}／{e(x.get('suggested_action', ''))}。" if x.get("ok")
                 else "一句話沒抽成結構化欄位（只存了原文）。")
    msg = (f'<p class="ok">已記錄：{e(VERDICTS.get(done["incident"], done["incident"]))}（{done["feedback"]} 筆回饋，{e(done["by"])}）。謝謝，這會讓下一次更準。{extra}</p>' if done
           else f'<p class="err">{e(error)}</p>' if error else "")
    checked = "".join(f'<label class="chk"><input type="checkbox" name="checked" value="{e(c)}"> {e(c)}</label>' for c in CHECKED)
    rca = "".join(f'<div class="v"><label>{e(q)}</label><br>' + "".join(f'<label><input type="radio" name="{k}" value="{v}"> {t}</label>' for v, t in RCA_USEFUL) + "</div>"
                  for k, q in RCA_QUESTIONS)
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>回饋：{e(d['service'] or '')}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;line-height:1.5}}
pre{{background:#f4f4f4;padding:10px;white-space:pre-wrap}}label{{display:block;margin-top:12px;font-weight:600}}
input[type=text],textarea,select{{width:100%;padding:6px;font-size:15px}}textarea{{height:64px}}.chk{{display:inline-block;font-weight:400;margin:4px 12px 0 0}}
.v label{{display:inline-block;margin-right:16px;font-weight:600}}button{{margin-top:16px;padding:10px 20px;font-size:16px}}
.ok{{background:#e6f7e6;padding:10px}}.err{{background:#fde8e8;padding:10px}}small{{color:#555}}.hint{{font-weight:400;color:#555;font-size:13px}}
#detail{{display:none}}{'body{max-width:none;margin:16px 24px}h3{border-top:2px solid #ddd;padding-top:12px;margin-top:28px}' if full else ''}</style></head><body>
<h2>{e(d['title'] or d['alert_id'])}</h2>
<pre>{e(d['summary'])}</pre>
{f'<p><b>AI 摘要</b>（只是參考）：<br>{e(d["llm"]).replace(chr(10), "<br>")}</p>' if d['llm'] else ''}
<p><b>過去相似的事故</b></p><ul>{sim}</ul>
{f'<h3>根因分析全文</h3><pre>{e(d.get("report") or "（沒有報告）")}</pre><h3>資深維運專家（老師傅）回饋</h3>' if full else f'<p><a href="/report?alert={e(d["alert_id"])}">根因報告全文</a></p>'}
{msg}
<form method="post" action="/feedback">
<input type="hidden" name="alert_id" value="{e(d['alert_id'])}">
{'<input type="hidden" name="full" value="1">' if full else ''}
{rca}
<div class="v"><label>這件是：</label><br>
{"".join(f'<label><input type="radio" name="verdict" value="{v}" required onclick="document.getElementById(\'detail\').style.display=(this.value==\'false_positive\')?\'none\':\'block\';document.getElementById(\'fp\').style.display=(this.value==\'false_positive\')?\'block\':\'none\'"> {t}</label>' for v, t in VERDICTS.items())}
</div>
<label>一句話說這件怎麼回事 <span class="hint">（{e(TEXT_HINT)}）</span><textarea name="feedback_text"></textarea></label>
<div id="fp" style="display:none"><label>為什麼不是問題 <select name="fp_reason">{_opt(FP_REASONS)}</select></label>
<label>補充 <span class="hint">（選填）</span><input type="text" name="fp_reason_text"></label></div>
<div id="detail">
<label>根因分類 {cat_hint}<select name="root_cause_category">{_opt([("", "—")] + CATEGORIES, d.get("suggested_category"))}</select></label>
<label>根因（一句話） <input type="text" name="root_cause" placeholder="例：Oracle 統計資訊過期，SELECT lots 走錯執行計畫"></label>
<label>處置（做了什麼讓它恢復） <span class="hint">（選填）</span><input type="text" name="action" placeholder="例：重跑 analyze，10 分鐘後恢復"></label>
<label>怎麼看出來的：看到什麼現象讓你想到是這個原因 <span class="hint">（這是最值錢的，請寫）</span>
<textarea name="how_detected" placeholder="例：只有 lots 那張表的查詢慢、其他 API 都正常，AWR 看到執行計畫變了"></textarea></label>
<label>查了哪些地方 <span class="hint">（可複選）</span></label>{checked}
<label>下次怎麼更快：缺什麼指標、規則、畫面 <span class="hint">（選填）</span><input type="text" name="improve"></label>
</div>
<label>你是 <input type="text" name="by" placeholder="名字或工號" required></label>
<label>備註 <span class="hint">（選填）</span><input type="text" name="note"></label>
<button type="submit">送出</button>
</form></body></html>"""


def render_report(alert: dict) -> str:
    return (f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><title>{html.escape(alert.get('title') or '')}</title></head>"
            f"<body style='font-family:system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px'>"
            f"<p><a href='/feedback?alert={html.escape(alert['alert_id'])}'>← 回饋</a></p><pre style='white-space:pre-wrap'>{html.escape(alert.get('report') or '（沒有報告）')}</pre></body></html>")


def to_json(x) -> bytes:
    return json.dumps(x, ensure_ascii=False, default=str).encode("utf-8")

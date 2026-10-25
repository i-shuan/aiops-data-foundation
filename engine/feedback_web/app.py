"""回饋表單的邏輯（不含 HTTP）：讀告警、組表單資料、寫回饋與事故。單元測試用假的 ES 測這裡。"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from core.es import doc_id
from detector.feedback import FEEDBACK_MAPPING, INCIDENT_MAPPING

ALERT_INDEX, FEEDBACK_INDEX, INCIDENT_INDEX = "aiops-alerts", "aiops-feedback", "aiops-incidents"
_ready = set()
VERDICTS = {"true_positive": "真的", "false_positive": "誤報", "wrong_root_cause": "真的，但根因報錯了"}
CATEGORIES = [("db", "DB（慢查詢、鎖、連線池、統計資訊）"), ("network", "網路（延遲、斷線、DNS）"), ("config_change", "設定變更"),
              ("deploy", "版本更新"), ("external", "外部系統（供應商、其他部門的服務）"), ("resource", "資源不足（CPU、記憶體、磁碟）"),
              ("bug", "程式錯誤"), ("data", "資料問題（髒資料、量暴增）"), ("other", "其他")]
CHECKED = ["Oracle AWR / 慢查詢", "DB 連線池", "主機 CPU / 記憶體 / 磁碟", "網路 / DNS", "Kibana log", "trace 瀑布圖",
           "Kafka lag", "近期的版本更新 / 設定變更", "上游 / 下游服務", "外部系統狀態"]
FP_REASONS = [("batch", "月底 / 定時批次本來就這樣"), ("loadtest", "壓測、演練"), ("known_slow", "這支 API 本來就慢"),
              ("low_volume", "量太少，幾筆就跳"), ("deploy", "換版期間"), ("other", "其他")]


def load_alert(es, alert_id: str) -> dict | None:
    got = es.req("GET", f"/{ALERT_INDEX}/_doc/{alert_id}", ok=(200, 404))
    return got.get("_source") if got.get("found") else None


def form_data(alert: dict) -> dict:
    """給表單用的預填資料。"""
    sim = alert.get("similar") or []
    return {"alert_id": alert["alert_id"], "title": alert.get("title"), "summary": alert.get("summary") or "", "service": alert.get("service"),
            "dept": alert.get("dept"), "severity": alert.get("severity"), "status": alert.get("status"), "started_at": alert.get("started_at"),
            "llm": alert.get("llm_summary") or "", "similar": sim[:3], "anomaly_ids": alert.get("anomaly_ids") or [],
            "series": alert.get("series") or [], "report_url": alert.get("report_url")}


def submit(es, form: dict, now: datetime | None = None) -> dict:
    """form：verdict、by、root_cause_category、root_cause、action、how_detected、checked（list）、improve、fp_reason、note。
    回傳 {"feedback": 寫了幾筆, "incident": 狀態}。"""
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
    es.req("POST", f"/{FEEDBACK_INDEX}/_refresh", ok=(200,))
    return {"feedback": len(docs), "incident": status, "by": by}


def _opt(options, selected=None):
    return "".join(f'<option value="{html.escape(v)}"{" selected" if v == selected else ""}>{html.escape(t)}</option>' for v, t in options)


def render_form(d: dict, done: dict | None = None, error: str | None = None) -> str:
    e = html.escape
    sim = "".join(f"<li><b>{e((s.get('start') or '')[:16])}</b> [{e(s.get('root_cause_category') or '—')}] {e(s.get('root_cause') or '')}"
                  f"<br><small>處置：{e(s.get('action') or '—')}｜怎麼看出來的：{e(s.get('how_detected') or '—')}｜為什麼像：{e('；'.join(s.get('why') or []))}</small></li>"
                  for s in d["similar"]) or "<li>沒有相似的過去事故</li>"
    msg = (f'<p class="ok">已記錄：{e(VERDICTS.get(done["incident"], done["incident"]))}（{done["feedback"]} 筆回饋，{e(done["by"])}）。謝謝，這會讓下一次更準。</p>' if done
           else f'<p class="err">{e(error)}</p>' if error else "")
    checked = "".join(f'<label class="chk"><input type="checkbox" name="checked" value="{e(c)}"> {e(c)}</label>' for c in CHECKED)
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>回饋：{e(d['service'] or '')}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;line-height:1.5}}
pre{{background:#f4f4f4;padding:10px;white-space:pre-wrap}}label{{display:block;margin-top:12px;font-weight:600}}
input[type=text],textarea,select{{width:100%;padding:6px;font-size:15px}}textarea{{height:64px}}.chk{{display:inline-block;font-weight:400;margin:4px 12px 0 0}}
.v label{{display:inline-block;margin-right:16px;font-weight:600}}button{{margin-top:16px;padding:10px 20px;font-size:16px}}
.ok{{background:#e6f7e6;padding:10px}}.err{{background:#fde8e8;padding:10px}}small{{color:#555}}.hint{{font-weight:400;color:#555;font-size:13px}}
#detail{{display:none}}</style></head><body>
<h2>{e(d['title'] or d['alert_id'])}</h2>
<pre>{e(d['summary'])}</pre>
{f'<p><b>AI 摘要</b>（只是參考）：<br>{e(d["llm"]).replace(chr(10), "<br>")}</p>' if d['llm'] else ''}
<p><b>過去相似的事故</b></p><ul>{sim}</ul>
{f'<p><a href="/report?alert={e(d["alert_id"])}">根因報告全文</a></p>'}
{msg}
<form method="post" action="/feedback">
<input type="hidden" name="alert_id" value="{e(d['alert_id'])}">
<div class="v"><label>這件是：</label><br>
{"".join(f'<label><input type="radio" name="verdict" value="{v}" required onclick="document.getElementById(\'detail\').style.display=(this.value==\'false_positive\')?\'none\':\'block\';document.getElementById(\'fp\').style.display=(this.value==\'false_positive\')?\'block\':\'none\'"> {t}</label>' for v, t in VERDICTS.items())}
</div>
<div id="fp" style="display:none"><label>為什麼不是問題 <select name="fp_reason">{_opt(FP_REASONS)}</select></label>
<label>補充 <span class="hint">（選填）</span><input type="text" name="fp_reason_text"></label></div>
<div id="detail">
<label>根因分類 <select name="root_cause_category">{_opt([("", "—")] + CATEGORIES)}</select></label>
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

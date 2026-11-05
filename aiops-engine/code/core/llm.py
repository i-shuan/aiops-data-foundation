"""LLM 摘要（RAG 的「生成」，docs/pdca/phase4-rca-llm/2026-10-llm-summary.md）：報告最前面三到五句，每一句附依據。

原則：
  - 只看輸入包（結構化、約 4,000 token）：這件告警、根因分析的前幾名、最像的過去事故。不丟原始 metrics、log。
  - 每一句結尾要有依據的 id（[T1] 這件告警、[A1] 異常、[S1] 慢的那段、[E1] 錯誤源頭、[L1] log 樣式、[I1] 過去事故）；引用了不存在的 id、或一句沒有依據 → 整段不顯示（程式檢查，不靠 LLM 自律）。
  - 只加註、不擋告警：LLM 掛了、逾時，報告照出，只是沒有這一段。

provider（環境變數 LLM_PROVIDER）：
  template   不叫模型：用固定句型從輸入包組出來（可測、沒有模型也能跑；也是其他 provider 失敗時的退路）
  anthropic  LLM_API_KEY、LLM_CHAT_MODEL、ANTHROPIC_BASE_URL（預設 https://api.anthropic.com）
  openai     LLM_API_KEY、LLM_CHAT_MODEL、OPENAI_BASE_URL（OpenAI 相容的 /v1/chat/completions）
"""
from __future__ import annotations

import json
import math
import os
import re

import requests

from . import plugin

CITE = re.compile(r"\[([A-Z]\d+)\]")
PROMPT = """你是工廠 IT 維運的值班助理。根據下面的輸入包，用繁體中文寫 3～5 句話：最可能的原因（只能從 segments、error_origins、new_patterns 推）、影響（哪些服務、API、多久）、建議先查哪一段。
過去的 case（similar、classification）只用來說「這件像哪一類」，不要照抄它們的處置當建議；程式可能已經換版，舊的處置不一定對。
規則：每一句結尾一定要放依據的 id，格式像 [S1] 或 [A1][I1]（講這件告警本身：服務、持續多久、嚴重度，引用 [T1]）；只能引用輸入包裡有的 id；不能自己編服務名、數字；不確定就說「不確定」。只輸出那幾句，一句一行，不要標題。

trace_ready 是 false：這個服務這段時間的 trace 還沒就緒（trace 合成晚 1～2 分鐘，剛開件時常見），不能寫「trace 沒看到變慢」；要寫「trace 還沒就緒，先不下結論」，建議看 log、稍後報告會重跑。

輸入包（JSON）：
"""


def _num(x):
    """輸入包裡的數字：NaN、None、不是數字的都變 None（根因分析沒有對照組時 delta_ms 是 NaN；round(nan) 會丟例外）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None   # NaN、±inf 都變 None


def _r(x):
    v = _num(x)
    return None if v is None else round(v)


def build_pack(alert: dict, findings: dict | None, similar: list[dict], classification: dict | None = None) -> dict:
    """輸入包：每個項目一個 id。classification 是呼叫端用 investigator.similar.classify(similar) 算好的（這裡不認得 investigator）；
    有 similar 時進輸入包的 classification（沒分類到就是 None）。"""
    f = findings or {}
    items = {}
    # 這件告警本身也要有 id（T1）：模型講「影響 mes-api、持續 10 分鐘」這種話要有依據可引用，
    # 不然沒有相似事故時它只能寫沒依據的句子，整段被擋（實測 gpt-6-luna 3 次 3 次都這樣）
    pack = {"alert": {"id": "T1", "alert_id": alert.get("alert_id"), "service": alert.get("service"), "severity": alert.get("severity"),
                      "started_at": str(alert.get("started_at") or "")[:16], "minutes": alert.get("minutes"),
                      "routes": alert.get("routes") or [], "dept": alert.get("dept")},
            "anomalies": [], "segments": [], "error_origins": [], "new_patterns": [], "similar": []}
    items["T1"] = alert
    for i, a in enumerate(f.get("anomalies") or [], 1):
        k = f"A{i}"; items[k] = a
        pack["anomalies"].append({"id": k, "series": a.get("series"), "value": _num(a.get("value")), "usual": _num(a.get("median")), "z": _num(a.get("z"))})
    for i, s in enumerate(f.get("segments") or [], 1):
        k = f"S{i}"; items[k] = s
        pack["segments"].append({"id": k, "segment": s["segment"], "now_ms": _r(s.get("now_ms")), "usual_ms": _r(s.get("ref_ms")),
                                 "extra_ms": _r(s.get("delta_ms"))})
    for i, e in enumerate(f.get("error_origins") or [], 1):
        k = f"E{i}"; items[k] = e
        pack["error_origins"].append({"id": k, "segment": e["segment"], "requests_with_error": e["now_errors"], "usual": e["ref_errors"]})
    for i, p in enumerate(f.get("new_patterns") or [], 1):
        k = f"L{i}"; items[k] = p
        pack["new_patterns"].append({"id": k, "service": p["service"], "pattern": p["pattern"][:120], "now": p["now"], "usual": p["ref"]})
    # 過去的 case 只給分類用：只放日期、分類、為什麼像，不放當時的根因、處置（docs/pdca/meta/2026-11-case-classify.md）
    for i, s in enumerate(similar or [], 1):
        k = f"I{i}"; items[k] = s
        pack["similar"].append({"id": k, "when": (s.get("start") or "")[:16], "root_cause_category": s.get("root_cause_category"),
                                "why_similar": s.get("why")})
    if similar:
        cls = classification
        pack["classification"] = {"category": cls["category"], "confidence": cls["confidence"], "votes": cls["votes"]} if cls else None
    # trace 還沒就緒：根因分析查過、但這個服務這段時間沒有任何 trace（trace 合成晚 1～2 分鐘）。沒查過（findings 是 None）不算
    req = f.get("requests")
    pack["trace_ready"] = not (isinstance(req, dict) and not req.get("requests"))
    pack["_ids"] = sorted(items)
    return pack


def validate(text: str, pack: dict) -> tuple[bool, str]:
    """每一句要有依據、依據要存在。回傳 (ok, 原因)。"""
    ids = set(pack.get("_ids") or [])
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return False, "空的"
    if len(lines) > 6:
        return False, f"{len(lines)} 句，太長"
    for l in lines:
        cites = CITE.findall(l)
        if not cites:
            return False, f"沒有依據：{l[:40]}"
        bad = [c for c in cites if c not in ids]
        if bad:
            return False, f"引用不存在的依據 {bad}：{l[:40]}"
    return True, ""


def template(pack: dict) -> str:
    """不叫模型的固定句型：從輸入包組 3～5 句，每句帶依據。"""
    a, out = pack["alert"], []
    seg, err, sim, pats, anoms = pack["segments"], pack["error_origins"], pack["similar"], pack["new_patterns"], pack["anomalies"]
    slow = seg[0] if seg and (seg[0]["extra_ms"] or 0) > 0 else None      # 第一段沒變慢（可能還變快）就不算「慢在哪」
    if slow:
        out.append(f"{a['service']} 變慢，主要多在 {slow['segment']}：每個{plugin.unit_text()}多花約 {slow['extra_ms']} 毫秒（平常 {slow['usual_ms']} 毫秒）[{slow['id']}]。")
    if err:
        e = err[0]
        out.append(f"錯誤最早出在 {e['segment']}（{e['requests_with_error']} 個{plugin.unit_text().rstrip()}，平常 {e['usual']}）[{e['id']}]。")
    if anoms and not (slow or err):
        x = anoms[0]
        usual = f"，平常 {x['usual']:.4g}" if x.get("usual") is not None else ""
        now_v = f"{x['value']:.4g}" if x.get("value") is not None else "（沒有值）"
        out.append(f"{a['service']} 的 {x['series']} 異常：現在 {now_v}{usual} [{x['id']}]。")
    if pats:
        p = pats[0]
        out.append(f"同時間 {p['service']} 新出現 log「{p['pattern'][:60]}」{p['now']} 次 [{p['id']}]。")
    if sim:
        s, cls = sim[0], pack.get("classification")
        ids = "".join(f"[{x['id']}]" for x in sim)
        if cls:
            out.append(f"依過去相似的 case，這件像 {cls['category']} 類（{len(sim)} 件裡佔 {cls['confidence']:.0%}；最像 {s['when']}："
                       f"{'；'.join(s.get('why_similar') or [])[:80]}）{ids}。")
        else:
            out.append(f"跟 {s['when']} 的事故相似（{'；'.join(s.get('why_similar') or [])[:80]}），但那件沒有分類 [{s['id']}]。")
        if slow or err:
            where = (slow["segment"] if slow else err[0]["segment"]).split(" › ")[-1]
            out.append(f"根因請以上面的定位為準，先查 {where} 這一段的下游 [{slow['id'] if slow else err[0]['id']}]。")
    elif slow or err:
        where = (slow["segment"] if slow else err[0]["segment"]).split(" › ")[-1]
        ref = slow["id"] if slow else err[0]["id"]
        out.append(f"沒有相似的過去事故；建議先查 {where} 這一段的下游（DB、外部系統）[{ref}]。")
    elif anoms and not pack.get("trace_ready", True):
        # 沒有 trace 資料不等於「沒變慢」：講清楚是還沒就緒，不下結論
        out.append(f"{a['service']} 這段時間還沒有 trace 可以看（trace 合成晚 1～2 分鐘），先不下結論；先看它本身的資源和 log，報告稍後會重跑 [{anoms[0]['id']}]。")
    elif anoms:
        out.append(f"沒有相似的過去事故，trace 也沒看到哪一段變慢或出錯；先看 {a['service']} 本身的資源和 log [{anoms[0]['id']}]。")
    return "\n".join(out[:5])


def _call_anthropic(prompt: str) -> str:
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    r = requests.post(f"{base}/v1/messages", timeout=60,
                      headers={"x-api-key": os.environ["LLM_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": os.environ.get("LLM_CHAT_MODEL", "claude-sonnet-4-5"), "max_tokens": 600,
                            "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return "".join(c.get("text", "") for c in r.json().get("content", []))


def _call_openai(prompt: str) -> str:
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
    body = {"model": os.environ.get("LLM_CHAT_MODEL", "gpt-4o-mini"), "messages": [{"role": "user", "content": prompt}]}
    headers = {"Authorization": f"Bearer {os.environ['LLM_API_KEY']}", "content-type": "application/json"}
    # 新的模型只收 max_completion_tokens、舊的只收 max_tokens：先用新的，被 400 說不支援就換舊的
    # 會推理的模型把預算花在推理上，600 不夠就回空的（實測 gpt-6-luna 3 次有 1 次）：給 2000
    r = requests.post(f"{base}/v1/chat/completions", timeout=60, headers=headers, json={**body, "max_completion_tokens": 2000})
    if r.status_code == 400 and "max_completion_tokens" in r.text:
        r = requests.post(f"{base}/v1/chat/completions", timeout=60, headers=headers, json={**body, "max_tokens": 600})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""


def summarize(pack: dict, provider: str | None = None) -> dict:
    """回傳 {"text", "provider", "ok", "reason"}。text 只有在 ok 時才該顯示。"""
    provider = provider or os.environ.get("LLM_PROVIDER") or "template"
    if provider == "template":
        text = template(pack)
        ok, reason = validate(text, pack)
        return {"text": text if ok else "", "provider": provider, "ok": ok, "reason": reason}
    try:
        prompt = PROMPT + json.dumps({k: v for k, v in pack.items() if k != "_ids"}, ensure_ascii=False)
        text = {"anthropic": _call_anthropic, "openai": _call_openai}[provider](prompt)
        if not text.strip():   # 回空的（推理模型預算用完、或被內容過濾）：跟掛了一樣退回固定句型
            text, provider = template(pack), f"template（{provider} 回空的）"
    except Exception as e:    # 模型掛了：退回固定句型，報告照出
        text, provider = template(pack), f"template（{provider} 失敗：{str(e)[:80]}）"
    ok, reason = validate(text, pack)
    return {"text": text if ok else "", "provider": provider, "ok": ok, "reason": reason}


# ── 回饋的自然語言抽取（docs/pdca/phase5-learning-loop/2026-10-feedback-nl-extract.md）──
# 值班寫一句話（「這是每月初的批次匯出報表，屬於預期內負載」），抽成四個欄位；抽出來的只寫進回饋和「待審核」的調整表，不自動生效。
# 跟摘要一樣的原則：模型回的要過程式檢查（validate_extraction），過不了就當沒抽到（只存原文），不讓回饋寫入失敗。

FEEDBACK_TYPES = ("false_positive", "true_positive", "wrong_root_cause")
SUGGESTED_ACTIONS = ("suppress_during_cron", "widen_threshold", "add_rule", "none")
# identified_cause 的代號（短、snake_case）。清單跟回饋表單的根因分類、誤報原因對得起來；都不像就 unknown
CAUSE_CODES = ("scheduled_batch_job", "load_test", "deploy", "config_change", "db_slow_query", "db_lock", "db_connection_pool",
               "network", "dns", "external_api", "downstream_dependency", "resource_cpu", "resource_memory", "disk_full", "bug",
               "data_volume", "dirty_data", "low_traffic", "known_slow", "traffic_spike", "cache_miss", "kafka_lag",
               "certificate_expired", "unknown")
CAUSE_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
EXTRACT_FIELDS = ("feedback_type", "identified_cause", "target_service", "suggested_action")

EXTRACT_PROMPT = """你是工廠 IT 維運的值班助理。值班對一件告警寫了一句回饋（繁體中文或英文），請抽成 JSON，只輸出一個 JSON 物件、不要別的字。

欄位（四個都要有）：
- feedback_type：false_positive（誤報、不是問題、預期內、壓測、演練、本來就這樣）、true_positive（真的事故）、wrong_root_cause（真的，但報告指的根因或對象錯了、「其實是…」）
- identified_cause：原因的代號，只能從這份清單挑一個：%s。
  scheduled_batch_job＝定時批次、排程、月初月底結帳、報表匯出；load_test＝壓測、演練、故意弄的；deploy＝換版、上線；config_change＝設定改錯；
  db_slow_query＝慢查詢、執行計畫、統計資訊、索引；db_lock＝鎖表；db_connection_pool＝連線池滿、逾時；network＝網路、掉封包、switch；dns＝DNS；
  external_api＝外部系統、供應商、第三方 API；downstream_dependency＝下游服務拖累（沒說是什麼問題）；resource_cpu／resource_memory／disk_full＝資源；
  bug＝程式錯誤、例外、重試迴圈；data_volume＝資料量暴增；dirty_data＝髒資料；low_traffic＝量太少幾筆就跳；known_slow＝這支本來就慢；
  traffic_spike＝活動、促銷流量；cache_miss＝快取失效；kafka_lag＝Kafka 落後；certificate_expired＝憑證過期；都不像＝unknown
- target_service：這句話在講哪個服務。預設是告警的服務（%s）；只有文字明確點名另一個服務（「其實是 wip-worker 的問題」「慢的是下游 oracle-proxy」）才換成那個，不要自己編。
- suggested_action：系統該做什麼，四選一：
  suppress_during_cron＝預期內的週期性負載（定時批次、排程、每天／每月固定時段），那段時間不該報；
  widen_threshold＝門檻太敏感、這支本來就慢、量太少幾筆就跳，要放寬；
  add_rule＝真的事故，而且值班說下次要直接報、要更早抓到、以後要能抓到；
  none＝其他（真的事故處理完、根因更正、一次性事件、沒提到要調整、明說不用調）。

告警：%s
回饋：%s
"""


def parse_json_object(text: str) -> dict | None:
    """模型回的字串裡撈出第一個 JSON 物件（容許 ```json 圍欄、前後多話）；撈不到回 None。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I)
    for cand in (s, s[s.find("{"): s.rfind("}") + 1] if "{" in s and "}" in s else ""):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def validate_extraction(obj, alert: dict | None = None) -> tuple[dict | None, str]:
    """四個欄位都要有、值要在允許的範圍。回傳 (乾淨的四欄, 原因)；不合格回 (None, 原因)。
    target_service 空的補告警的服務（「從告警帶入、文字有提到就覆蓋」）；identified_cause 大小寫、空白、減號正規化成 snake_case。"""
    if not isinstance(obj, dict):
        return None, "不是 JSON 物件"
    missing = [f for f in EXTRACT_FIELDS if f not in obj]
    if missing:
        return None, f"缺欄位 {missing}"
    ft, act = str(obj["feedback_type"] or "").strip().lower(), str(obj["suggested_action"] or "").strip().lower()
    if ft not in FEEDBACK_TYPES:
        return None, f"feedback_type 不認得：{ft[:40]}"
    if act not in SUGGESTED_ACTIONS:
        return None, f"suggested_action 不認得：{act[:40]}"
    cause = re.sub(r"[\s\-]+", "_", str(obj["identified_cause"] or "").strip().lower())
    if not CAUSE_RE.match(cause):
        return None, f"identified_cause 不是短代號：{str(obj['identified_cause'])[:40]}"
    svc = str(obj["target_service"] or "").strip() or str((alert or {}).get("service") or "").strip()
    if not svc or not SERVICE_RE.match(svc):
        return None, f"target_service 不像服務名：{svc[:40]}"
    return {"feedback_type": ft, "identified_cause": cause, "target_service": svc, "suggested_action": act}, ""


# 不叫模型的退路：關鍵字表（繁體中文為主、夾英文）。順序有意義：前面的先命中
_FP = ("誤報", "不是問題", "不算", "預期內", "預期中", "正常的", "本來就", "壓測", "演練", "不用報", "不該報", "不是我們", "false positive",
       "not an issue", "not a problem", "expected", "always slow", "load test", "drill")
_WRONG = ("根因報錯", "根因錯", "根因不對", "報錯對象", "報錯了", "其實是", "其實不是", "root cause was wrong", "wrong root cause")
_CAUSES = [("scheduled_batch_job", ("批次", "排程", "月底", "月初", "結帳", "匯出報表", "每天凌晨", "batch", "etl", "nightly", "cron", "scheduled")),
           ("load_test", ("壓測", "壓力測試", "演練", "故意", "load test", "stress test", "drill")),
           ("deploy", ("換版", "上線", "部署", "deploy", "release", "rollout")),
           ("config_change", ("設定改", "設定錯", "改設定", "參數調", "config", "misconfig")),
           ("db_connection_pool", ("連線池", "connection pool", "pool exhausted", "pool size")),
           ("db_lock", ("鎖表", "死鎖", "lock")),
           ("db_slow_query", ("統計資訊", "執行計畫", "慢查詢", "索引", "analyze", "slow query", "execution plan", "full scan")),
           ("dns", ("dns",)),
           ("certificate_expired", ("憑證", "certificate", "tls", "ssl")),
           ("kafka_lag", ("kafka", "consumer", "落後", "lag")),
           ("network", ("網路", "掉封包", "switch", "斷線", "network", "packet loss")),
           ("external_api", ("供應商", "外部系統", "第三方", "外部中斷", "external", "vendor", "third-party", "third party")),
           ("resource_memory", ("記憶體", "oom", "memory", "heap")),
           ("resource_cpu", ("cpu",)),
           ("disk_full", ("磁碟滿", "磁碟", "disk full", "disk")),
           ("bug", ("bug", "例外", "exception", "null", "重試", "程式錯")),
           ("data_volume", ("量暴增", "資料量", "萬筆", "大量資料", "data volume", "backfill")),
           ("dirty_data", ("髒資料", "壞資料", "dirty data", "bad data")),
           ("low_traffic", ("量太少", "幾筆", "只有幾", "low traffic", "few requests")),
           ("known_slow", ("本來就慢", "本來就這樣", "一直都慢", "always slow", "known slow")),
           ("traffic_spike", ("促銷", "活動", "流量翻倍", "流量暴增", "promotion", "campaign", "traffic spike")),
           ("cache_miss", ("快取", "cache")),
           ("downstream_dependency", ("下游", "拖累", "被拖", "downstream"))]
_CRON = ("批次", "排程", "月底", "月初", "每天", "每月", "每週", "固定時段", "那個時段", "batch", "etl", "nightly", "cron", "scheduled", "every night", "window")
_WIDEN = ("放寬", "門檻", "太敏感", "本來就慢", "本來就這樣", "量太少", "幾筆", "threshold", "too tight", "too sensitive", "always slow")
_RULE = ("直接告警", "直接報", "以後要", "之後要", "下次要", "要能抓", "更早", "加規則", "加一條", "alert directly", "add a rule", "catch it next time",
         "should alert", "earlier")
_NO_TUNE = ("不用調", "不需要調", "處理完", "已處理", "已恢復", "no change needed", "no tuning")
_SVC_CUE = ("其實是", "根因在", "問題在", "慢的是", "是下游", "下游", "造成", "來自", "的連線池", "的問題", "on ", "downstream", "actually", "caused by",
            "root cause is", "it was")
_SVC_TOKEN = re.compile(r"(?<![A-Za-z0-9._-])([a-z][a-z0-9]*(?:-[a-z0-9]+)+)(?![A-Za-z0-9._-])")


def template_extract(text: str, alert: dict | None = None) -> dict:
    """關鍵字版的抽取（LLM_PROVIDER=template，或沒有模型時）。只是退路，準不準在 tests/feedback_extract_eval.py 量。"""
    t, low = text or "", (text or "").lower()
    svc0 = str((alert or {}).get("service") or "")
    ft = ("wrong_root_cause" if any(k in low for k in _WRONG) else "false_positive" if any(k in low for k in _FP) else "true_positive")
    cause = next((c for c, keys in _CAUSES if any(k in low for k in keys)), "unknown")
    if any(k in low for k in _NO_TUNE) and not any(k in low for k in _RULE):
        act = "none"
    elif cause == "scheduled_batch_job" or (ft == "false_positive" and any(k in low for k in _CRON)):
        act = "suppress_during_cron"
    elif ft == "false_positive" and any(k in low for k in _WIDEN):
        act = "widen_threshold"
    elif ft != "false_positive" and any(k in low for k in _RULE):
        act = "add_rule"
    else:
        act = "none"
    svc = svc0
    if any(k in low for k in _SVC_CUE):
        other = [m for m in _SVC_TOKEN.findall(t) if m != svc0]
        if other:
            svc = other[0]
    return {"feedback_type": ft, "identified_cause": cause, "target_service": svc, "suggested_action": act}


def extract_feedback(text: str, alert: dict | None = None, provider: str | None = None) -> dict:
    """回傳 {"ok", "fields", "provider", "reason", "raw"}。ok 才有 fields（四欄）；不 ok 就只存原文、不抽取。
    模型掛了（連不上、回空的）退回關鍵字版（provider 會寫明）；模型回了但格式不對（驗不過）就不抽，不猜。永不丟例外。"""
    alert = alert or {}
    provider = provider or os.environ.get("LLM_PROVIDER") or "template"
    text = (text or "").strip()
    if not text:
        return {"ok": False, "fields": None, "provider": provider, "reason": "沒有文字", "raw": ""}
    raw = ""
    try:
        if provider == "template":
            obj = template_extract(text, alert)
        else:
            ctx = {"service": alert.get("service"), "series": alert.get("series") or [], "title": alert.get("title")}
            prompt = EXTRACT_PROMPT % ("、".join(CAUSE_CODES), alert.get("service") or "（不明）", json.dumps(ctx, ensure_ascii=False), text)
            try:
                raw = {"anthropic": _call_anthropic, "openai": _call_openai}[provider](prompt)
            except Exception as e:    # 模型掛了：跟摘要一樣退回不叫模型的版本
                raw, obj, provider = "", template_extract(text, alert), f"template（{provider} 失敗：{str(e)[:80]}）"
            else:
                if not raw.strip():
                    obj, provider = template_extract(text, alert), f"template（{provider} 回空的）"
                else:
                    obj = parse_json_object(raw)
                    if obj is None:
                        return {"ok": False, "fields": None, "provider": provider, "reason": f"回的不是 JSON：{raw[:60]}", "raw": raw[:500]}
        fields, why = validate_extraction(obj, alert)
    except Exception as e:    # noqa: BLE001  抽取只是加值，任何意外都不能讓回饋寫入失敗
        return {"ok": False, "fields": None, "provider": provider, "reason": f"{type(e).__name__}：{str(e)[:80]}", "raw": raw[:500]}
    return {"ok": fields is not None, "fields": fields, "provider": provider, "reason": why, "raw": raw[:500]}

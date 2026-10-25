"""LLM 摘要（RAG 的「生成」，docs/pdca/2026-10-llm-summary.md）：報告最前面三到五句，每一句附依據。

原則：
  - 只看輸入包（結構化、約 4,000 token）：這件告警、根因分析的前幾名、最像的過去事故。不丟原始 metrics、log。
  - 每一句結尾要有依據的 id（[A1]、[S1]、[E1]、[L1]、[I1]）；引用了不存在的 id、或一句沒有依據 → 整段不顯示（程式檢查，不靠 LLM 自律）。
  - 只加註、不擋告警：LLM 掛了、逾時，報告照出，只是沒有這一段。

provider（環境變數 LLM_PROVIDER）：
  template   不叫模型：用固定句型從輸入包組出來（可測、沒有模型也能跑；也是其他 provider 失敗時的退路）
  anthropic  LLM_API_KEY、LLM_CHAT_MODEL、ANTHROPIC_BASE_URL（預設 https://api.anthropic.com）
  openai     LLM_API_KEY、LLM_CHAT_MODEL、OPENAI_BASE_URL（OpenAI 相容的 /v1/chat/completions）
"""
from __future__ import annotations

import json
import os
import re

import requests

CITE = re.compile(r"\[([A-Z]\d+)\]")
PROMPT = """你是工廠 IT 維運的值班助理。根據下面的輸入包，用繁體中文寫 3～5 句話：最可能的原因、影響（哪些服務、API、多久）、建議先做什麼（有過去事故就引用它的處置）。
規則：每一句結尾一定要放依據的 id，格式像 [S1] 或 [A1][I1]；只能引用輸入包裡有的 id；不能自己編服務名、數字；不確定就說「不確定」。只輸出那幾句，一句一行，不要標題。

輸入包（JSON）：
"""


def build_pack(alert: dict, findings: dict | None, similar: list[dict]) -> dict:
    """輸入包：每個項目一個 id。"""
    f = findings or {}
    items = {}
    pack = {"alert": {"id": alert.get("alert_id"), "service": alert.get("service"), "severity": alert.get("severity"),
                      "started_at": str(alert.get("started_at") or "")[:16], "minutes": alert.get("minutes"),
                      "routes": alert.get("routes") or [], "dept": alert.get("dept")},
            "anomalies": [], "segments": [], "error_origins": [], "new_patterns": [], "similar": []}
    for i, a in enumerate(f.get("anomalies") or [], 1):
        k = f"A{i}"; items[k] = a
        pack["anomalies"].append({"id": k, "series": a.get("series"), "value": a.get("value"), "usual": a.get("median"), "z": a.get("z")})
    for i, s in enumerate(f.get("segments") or [], 1):
        k = f"S{i}"; items[k] = s
        pack["segments"].append({"id": k, "segment": s["segment"], "now_ms": round(s["now_ms"]), "usual_ms": round(s["ref_ms"]),
                                 "extra_ms": round(s["delta_ms"])})
    for i, e in enumerate(f.get("error_origins") or [], 1):
        k = f"E{i}"; items[k] = e
        pack["error_origins"].append({"id": k, "segment": e["segment"], "requests_with_error": e["now_errors"], "usual": e["ref_errors"]})
    for i, p in enumerate(f.get("new_patterns") or [], 1):
        k = f"L{i}"; items[k] = p
        pack["new_patterns"].append({"id": k, "service": p["service"], "pattern": p["pattern"][:120], "now": p["now"], "usual": p["ref"]})
    for i, s in enumerate(similar or [], 1):
        k = f"I{i}"; items[k] = s
        pack["similar"].append({"id": k, "when": (s.get("start") or "")[:16], "root_cause_category": s.get("root_cause_category"),
                                "root_cause": s.get("root_cause"), "action": s.get("action"), "how_detected": s.get("how_detected"),
                                "why_similar": s.get("why")})
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
    if seg and seg[0]["extra_ms"] > 0:
        s = seg[0]
        out.append(f"{a['service']} 變慢，主要多在 {s['segment']}：每個請求多花約 {s['extra_ms']} 毫秒（平常 {s['usual_ms']} 毫秒）[{s['id']}]。")
    if err:
        e = err[0]
        out.append(f"錯誤最早出在 {e['segment']}（{e['requests_with_error']} 個請求，平常 {e['usual']}）[{e['id']}]。")
    if anoms and not (seg or err):
        x = anoms[0]
        usual = f"，平常 {x['usual']:.4g}" if isinstance(x.get("usual"), (int, float)) else ""
        out.append(f"{a['service']} 的 {x['series']} 異常：現在 {x['value']:.4g}{usual} [{x['id']}]。")
    if pats:
        p = pats[0]
        out.append(f"同時間 {p['service']} 新出現 log「{p['pattern'][:60]}」{p['now']} 次 [{p['id']}]。")
    if sim:
        s = sim[0]
        cat = f"[{s['root_cause_category']}] " if s.get("root_cause_category") else ""
        act = f"，當時的處置：{s['action']}" if s.get("action") else ""
        out.append(f"跟 {s['when']} 的事故相似（{'；'.join(s.get('why_similar') or [])[:80]}），當時的根因是 {cat}{s.get('root_cause') or '未記錄'}{act} [{s['id']}]。")
        out.append(f"建議先照上次的方式確認：{s.get('how_detected') or '看同一段的指標'} [{s['id']}]。")
    else:
        if seg or err:
            where = (seg[0]["segment"] if seg else err[0]["segment"]).split(" › ")[-1]
            ref = seg[0]["id"] if seg else err[0]["id"]
            out.append(f"沒有相似的過去事故；建議先查 {where} 這一段的下游（DB、外部系統）[{ref}]。")
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
    r = requests.post(f"{base}/v1/chat/completions", timeout=60,
                      headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}", "content-type": "application/json"},
                      json={"model": os.environ.get("LLM_CHAT_MODEL", "gpt-4o-mini"), "max_tokens": 600,
                            "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


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
    except Exception as e:    # 模型掛了：退回固定句型，報告照出
        text, provider = template(pack), f"template（{provider} 失敗：{str(e)[:80]}）"
    ok, reason = validate(text, pack)
    return {"text": text if ok else "", "provider": provider, "ok": ok, "reason": reason}

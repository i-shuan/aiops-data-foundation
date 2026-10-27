"""相似事故檢索（RAG 的「檢索」，docs/pdca/phase4-rca-llm/2026-10-similar-incidents.md）。

一件新的告警 → 指紋（服務、API、慢在哪一段、錯從哪開始、異常的指標組合、新 log 樣式）→ 去 aiops-incidents 找最像的幾件
→ 附上當時的根因、處置、怎麼看出來的，以及**為什麼像**（哪幾點相同）。

先用欄位比對（ES 的 bool／terms），不用向量：結果看得出為什麼像、不用模型、20～30 件就夠。
只檢索老師傅確認過的（status confirmed、corrected）；自動產生、還沒人看的不拿來教人。
"""
from __future__ import annotations

import re

INCIDENT_INDEX = "aiops-incidents"
# 比對權重（先估的）：同一段變慢、同一個錯誤源頭最有鑑別力；同服務其次；指標組合、log 樣式較弱
WEIGHTS = {"slow_segments": 4.0, "error_origins": 4.0, "services": 3.0, "routes": 2.0, "patterns": 2.0, "series": 1.0}
MIN_SCORE = 4.0        # 低於這個分數不算像：至少一個強特徵、或兩三個弱特徵
TOP_K = 3
CONFIRMED = ("confirmed", "corrected")


def _seg_name(segment: str) -> str:
    """段的名字去掉數字（SQL 裡的 id、批號），同一種呼叫才對得起來。"""
    return re.sub(r"\d+", "#", segment or "").strip()


def fingerprint(alert: dict, findings: dict | None) -> dict:
    """一件事的指紋：全部是 keyword 陣列，存進 aiops-incidents.fingerprint，也拿來查。"""
    f = findings or {}
    services = [alert.get("service")] if alert.get("service") else []
    services += [s.split(" › ")[0] for s in (d["segment"] for d in f.get("segments", [])[:3])]
    return {
        "services": sorted({s for s in services if s}),
        "routes": sorted(set(alert.get("routes") or ([f["route"]] if f.get("route") else []))),
        "series": sorted(set(alert.get("series") or [a["series"] for a in f.get("anomalies", []) if a.get("series")])),
        "slow_segments": [_seg_name(d["segment"]) for d in f.get("segments", []) if d.get("delta_ms", 0) > 0][:3],
        "error_origins": [_seg_name(d["segment"]) for d in f.get("error_origins", [])][:3],
        "patterns": [p["pattern"][:120] for p in f.get("new_patterns", []) if p.get("is_new")][:5],
    }


def query(fp: dict, exclude_id: str | None = None, size: int = 20) -> dict:
    should = []
    for field, w in WEIGHTS.items():
        vals = fp.get(field) or []
        if not vals:
            continue
        if field == "patterns":
            should += [{"match": {"fingerprint.patterns": {"query": v, "boost": w}}} for v in vals]
        else:
            should.append({"terms": {f"fingerprint.{field}": vals, "boost": w}})
    q = {"bool": {"filter": [{"terms": {"status": list(CONFIRMED)}}], "should": should, "minimum_should_match": 1}}
    if exclude_id:
        q["bool"]["must_not"] = [{"ids": {"values": [exclude_id]}}]
    return {"size": size, "query": q}


def explain(fp: dict, other: dict) -> tuple[float, list[str]]:
    """自己算分數（不用 ES 的 _score，才解釋得出為什麼像）：每個欄位有交集就加權重，並寫出交集的內容。"""
    ofp = other.get("fingerprint") or {}
    score, why = 0.0, []
    labels = {"services": "同服務", "routes": "同一支 API", "slow_segments": "同一段變慢", "error_origins": "錯誤源頭相同",
              "patterns": "同樣的新 log 樣式", "series": "同樣的指標異常"}
    for field, w in WEIGHTS.items():
        mine, theirs = set(fp.get(field) or []), set(ofp.get(field) or [])
        hit = mine & theirs
        if hit:
            score += w * (1 if field != "series" else min(len(hit), 3))
            why.append(f"{labels[field]}（{'、'.join(sorted(hit)[:3])}）")
    return score, why


def search(es, fp: dict, exclude_id: str | None = None, k: int = TOP_K, min_score: float = MIN_SCORE) -> list[dict]:
    """最像的 k 件（分數 ≥ min_score）。回傳給報告用的欄位＋為什麼像。"""
    if not any(fp.get(f) for f in WEIGHTS):
        return []
    hits = es.search(INCIDENT_INDEX, query(fp, exclude_id)).get("hits", {}).get("hits", [])
    out = []
    for h in hits:
        s = h["_source"]
        score, why = explain(fp, s)
        if score < min_score:
            continue
        out.append({"incident_id": h["_id"], "score": score, "why": why, "title": s.get("title"), "start": s.get("start"),
                    "services": s.get("services"), "root_cause_category": s.get("root_cause_category"), "root_cause": s.get("root_cause"),
                    "action": s.get("action"), "how_detected": s.get("how_detected"), "checked": s.get("checked"), "by": s.get("by"),
                    "status": s.get("status")})
    out.sort(key=lambda x: (-x["score"], x.get("start") or ""))
    return out[:k]


def format_similar(items: list[dict], tz_label: str = "") -> list[str]:
    """報告最前面那一段（Markdown）。"""
    if not items:
        return []
    lines = ["## 過去相似的事故（老師傅確認過的）", ""]
    for i, it in enumerate(items, 1):
        when = (it.get("start") or "")[:16].replace("T", " ")
        lines.append(f"{i}. **{when}** {it.get('title') or '、'.join(it.get('services') or [])}（為什麼像：{'；'.join(it['why'])}）")
        if it.get("root_cause"):
            lines.append(f"   - 當時的根因：{'[' + it['root_cause_category'] + '] ' if it.get('root_cause_category') else ''}{it['root_cause']}")
        if it.get("action"):
            lines.append(f"   - 處置：{it['action']}")
        if it.get("how_detected"):
            lines.append(f"   - 怎麼看出來的：{it['how_detected']}")
        if it.get("checked"):
            lines.append(f"   - 當時查了：{'、'.join(it['checked']) if isinstance(it['checked'], list) else it['checked']}")
        if it.get("by"):
            lines.append(f"   - 處理的人：{it['by']}")
    lines.append("")
    return lines


def evaluate(es, k: int = TOP_K, min_score: float = MIN_SCORE) -> dict:
    """留一法：每件已確認、有根因分類的事故輪流當「新的」，用它的指紋查其他的，看同一個根因分類有沒有排在前 k（recall@k）。"""
    hits = es.search(INCIDENT_INDEX, {"size": 1000, "query": {"bool": {"filter": [{"terms": {"status": list(CONFIRMED)}},
                                                                                     {"exists": {"field": "root_cause_category"}},
                                                                                     {"exists": {"field": "fingerprint"}}]}}}).get("hits", {}).get("hits", [])
    rows, hit = [], 0
    for h in hits:
        s = h["_source"]
        found = search(es, s["fingerprint"], exclude_id=h["_id"], k=k, min_score=min_score)
        ok = any(f.get("root_cause_category") == s["root_cause_category"] for f in found)
        hit += ok
        rows.append({"id": h["_id"], "title": s.get("title"), "category": s["root_cause_category"], "hit": ok,
                     "found": [(f["incident_id"], f.get("root_cause_category"), f["score"]) for f in found]})
    return {"n": len(rows), "hit": hit, "recall": (hit / len(rows)) if rows else None, "rows": rows}

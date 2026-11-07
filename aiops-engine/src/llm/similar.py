"""相似事故檢索（RAG 的「檢索」，docs/pdca/phase4-rca-llm/2026-10-similar-incidents.md）。

一件新的告警 → 指紋（服務、API、慢在哪一段、錯從哪開始、異常的指標組合、新 log 樣式）→ 去 aiops-incidents 找最像的幾件
→ **只拿來分類**：前 k 件以分數投票，說這件「像哪一類」（db、network、config_change…）＋信心（classify），
以及**為什麼像**（哪幾點相同）。不拿它們的根因、處置當建議：程式換版後指紋還對得上、根因可能已經不是那個
（docs/pdca/meta/2026-11-case-classify.md）。根因定位從當下的 trace 算（investigator/analyze.py），不靠 case。

先用欄位比對（ES 的 bool／terms），不用向量：結果看得出為什麼像、不用模型、20～30 件就夠。
只檢索老師傅確認過的（status confirmed、corrected）；自動產生、還沒人看的不拿來教人。
權重手設（WEIGHTS）；case ≥ 50 件時 learn_weights 用留一法準確率學，人決定要不要寫進 rca.yaml 的 similar_weights。
"""
from __future__ import annotations

from common.types.incident import DEFAULT_WEIGHTS as WEIGHTS, Fingerprint, Incident, classify, describe_class, seg_name  # noqa: F401
MIN_SCORE = 4.0        # 低於這個分數不算像：至少一個強特徵、或兩三個弱特徵
TOP_K = 3
# 跟資料庫要幾件候選再自己重算分數：資料庫照權重排（series 只算一次），explain 的 series 是 min(交集,3) 倍，
# 排序有一點不一樣，候選太少可能截掉本地分數最高的。已確認的事故預期幾十到一兩百件，explain 是 O(n)、很便宜，直接要 200。
CANDIDATES = 200
CONFIRMED = ("confirmed", "corrected")


_seg_name = seg_name   # 舊名字（importer 以前從這裡 import）


def fingerprint(alert: dict, findings: dict | None) -> Fingerprint:
    """一件事的指紋（core.incident.Fingerprint）：存進 aiops-incidents.fingerprint（to_doc），也拿來查。"""
    return Fingerprint.from_alert(alert, findings)


def explain(fp: Fingerprint | dict, other: dict, weights: dict | None = None) -> tuple[float, list[str]]:
    """自己算分數（不用 ES 的 _score，才解釋得出為什麼像）：每個欄位有交集就加權重，並寫出交集的內容。
    other 是候選事故的文件（dict）；weights：rca.yaml 的 similar_weights 或 learn_weights 學出來的；沒給用 WEIGHTS。"""
    fp, ofp = Fingerprint.of(fp), Fingerprint.of(other.get("fingerprint"))
    score, why = 0.0, []
    labels = {"services": "同服務", "routes": "同一支 API", "slow_segments": "同一段變慢", "error_origins": "錯誤源頭相同",
              "patterns": "同樣的新 log 樣式", "series": "同樣的指標異常"}
    for field, w in (weights or WEIGHTS).items():
        mine, theirs = set(fp.get(field)), set(ofp.get(field))
        hit = mine & theirs
        if hit:
            score += w * (1 if field != "series" else min(len(hit), 3))
            why.append(f"{labels[field]}（{'、'.join(sorted(hit)[:3])}）")
    return score, why


def rank(fp: Fingerprint | dict, candidates: list[tuple[str, dict]], k: int = TOP_K, min_score: float = MIN_SCORE,
         weights: dict | None = None) -> list[dict]:
    """M5 的核心（不碰 ES）：候選事故 [(id, 文件), …] 各自算分數（explain），留 ≥ min_score 的前 k 件，附為什麼像。
    候選要先只留老師傅確認過的（status 在 CONFIRMED）：ES 的 query 已經過濾；自己準備候選時用 confirmed_only。
    候選文件用 Incident.from_doc 讀（舊文件缺欄位給預設值）。"""
    fp = Fingerprint.of(fp)
    if fp.empty:
        return []
    out = []
    for _id, s in candidates:
        score, why = explain(fp, s, weights)
        if score < min_score:
            continue
        inc = Incident.from_doc(s)
        out.append({"incident_id": _id, "score": score, "why": why, "title": inc.title, "start": inc.start,
                    "services": inc.services, "root_cause_category": inc.root_cause_category, "root_cause": inc.root_cause,
                    "action": inc.action, "how_detected": inc.how_detected, "checked": inc.checked, "by": inc.by,
                    "status": inc.status})
    out.sort(key=lambda x: (-x["score"], x.get("start") or ""))
    return out[:k]


def confirmed_only(candidates: list[tuple[str, dict]], exclude_id: str | None = None) -> list[tuple[str, dict]]:
    return [(i, s) for i, s in candidates if s.get("status") in CONFIRMED and i != exclude_id]


def search(repo, fp: Fingerprint | dict, exclude_id: str | None = None, k: int = TOP_K, min_score: float = MIN_SCORE, size: int = CANDIDATES,
           weights: dict | None = None) -> list[dict]:
    """最像的 k 件（分數 ≥ min_score）。回傳給報告用的欄位＋為什麼像。size：跟資料庫要幾件候選（見 CANDIDATES）。
    候選由 repo.incidents.similar 給（PostgreSQL 照同一組權重先排、取前 size 件），分數一樣用 rank 算。"""
    fp = Fingerprint.of(fp)
    if fp.empty:
        return []
    return rank(fp, repo.incidents.similar(fp, CONFIRMED, exclude_id, size, weights=weights), k, min_score, weights)


def format_similar(items: list[dict], tz_label: str = "", cls: dict | None = None) -> list[str]:
    """報告最前面那一段（Markdown）：只給分類和「歷史上也發生過」的清單（日期、標題、為什麼像），不列當時的根因、處置：
    程式換版後指紋還對得上、根因可能已經不是那個，舊藥方不給新病。"""
    if not items:
        return []
    cls = cls if cls is not None else classify(items)
    lines = [f"## 分類（依過去相似的 case）：{describe_class(cls)}", ""]
    for i, it in enumerate(items, 1):
        when = (it.get("start") or "")[:16].replace("T", " ")
        cat = f"[{it['root_cause_category']}] " if it.get("root_cause_category") else ""
        lines.append(f"{i}. **{when}** {cat}{it.get('title') or '、'.join(it.get('services') or [])}（為什麼像：{'；'.join(it['why'])}）")
    lines.append("")
    return lines


def _confirmed_cases(repo) -> list[tuple[str, dict]]:
    """已確認、有根因分類、有指紋的事故（留一法的樣本）。"""
    return repo.incidents.find(CONFIRMED, having=("root_cause_category", "fingerprint"))


def evaluate_cases(cases: list[tuple[str, dict]], k: int = TOP_K, min_score: float = MIN_SCORE, weights: dict | None = None) -> dict:
    """留一法（不碰 ES）：每件輪流當「新的」，用其他件分類。recall@k：同分類有沒有排進前 k；accuracy：投票結果等不等於它的分類。"""
    rows, hit, right = [], 0, 0
    for _id, s in cases:
        found = rank(s["fingerprint"], [(i, o) for i, o in cases if i != _id], k, min_score, weights)
        ok = any(f.get("root_cause_category") == s["root_cause_category"] for f in found)
        cls = classify(found)
        correct = bool(cls) and cls["category"] == s["root_cause_category"]
        hit += ok
        right += correct
        rows.append({"id": _id, "title": s.get("title"), "category": s["root_cause_category"], "hit": ok,
                     "predicted": cls["category"] if cls else None, "confidence": cls["confidence"] if cls else None, "correct": correct,
                     "found": [(f["incident_id"], f.get("root_cause_category"), f["score"]) for f in found]})
    n = len(rows)
    return {"n": n, "hit": hit, "recall": (hit / n) if n else None, "correct": right, "accuracy": (right / n) if n else None, "rows": rows}


def evaluate(repo, k: int = TOP_K, min_score: float = MIN_SCORE, weights: dict | None = None) -> dict:
    """留一法：每件已確認、有根因分類的事故輪流當「新的」（recall@k 與分類準確率，見 evaluate_cases）。"""
    return evaluate_cases(_confirmed_cases(repo), k, min_score, weights)


MIN_CASES_TO_LEARN = 50
WEIGHT_GRID = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0)


def learn_weights(cases: list[tuple[str, dict]], start: dict | None = None, min_cases: int = MIN_CASES_TO_LEARN,
                  k: int = TOP_K, min_score: float = MIN_SCORE, rounds: int = 3) -> dict:
    """座標上升學特徵權重：每個欄位的權重在 WEIGHT_GRID 裡試，留一法準確率變好才換；全部欄位掃一輪沒變就停。
    case 少於 min_cases 不學（會過擬合），回 {"ok": False, "reason"}。回傳 {"ok", "weights", "before", "after", "n"}。"""
    if len(cases) < min_cases:
        return {"ok": False, "reason": f"已確認、有分類的 case 只有 {len(cases)} 件，少於 {min_cases} 件不學（維持手設的權重）", "n": len(cases)}
    w = dict(start or WEIGHTS)
    before = evaluate_cases(cases, k, min_score, w)["accuracy"]
    best = before
    for _ in range(rounds):
        changed = False
        for field in list(w):
            for v in WEIGHT_GRID:
                if v == w[field]:
                    continue
                trial = {**w, field: v}
                acc = evaluate_cases(cases, k, min_score, trial)["accuracy"]
                if acc > best + 1e-9:
                    w, best, changed = trial, acc, True
        if not changed:
            break
    return {"ok": True, "weights": w, "before": before, "after": best, "n": len(cases)}


# ── 歷史 case 補根因分析：線索排順序、先查什麼（docs/pdca/meta/2026-11-cases-tier-drop.md）──
# 當下的 trace 回答「哪裡壞了」；case 補「過去哪一段常出事、出事時老師傅先查什麼」。
# 只把**當下資料已經證實有變化**的段拿來排序（變慢、錯變多）；過去常出事、但現在沒變化的段不列——舊答案不取代證據。

MAX_HINTS = 3


def confirmed_cases(repo) -> list[tuple[str, dict]]:
    """已確認的事故（線索、先查清單用）：有指紋或有查過什麼的都算。"""
    return repo.incidents.find(CONFIRMED)


def segment_hints(findings: dict | None, cases: list[tuple[str, dict]], k: int = MAX_HINTS) -> list[dict]:
    """findings：這次根因分析的 segments（delta_ms > 0 的才算變慢）、error_origins；cases：已確認的事故 [(id, 文件)]。
    回傳 [{segment, kind（slow／error）, evidence, cases: [{id, title, start, category}]}]：過去出現過的件數多的排前面，
    一樣多照這次變化的大小（findings 本來的順序）。過去沒出現過的不列。"""
    f = findings or {}
    now = [("slow", d["segment"], f"每個多 {d['delta_ms']:.0f} ms（平常 {d['ref_ms']:.0f} ms）")
           for d in f.get("segments") or [] if (d.get("delta_ms") or 0) > 0]
    now += [("error", d["segment"], f"錯 {d['now_errors']} 個（平常 {d['ref_errors']}）") for d in f.get("error_origins") or []]
    field = {"slow": "slow_segments", "error": "error_origins"}
    out = []
    for order, (kind, seg, evidence) in enumerate(now):
        name = seg_name(seg)
        hit = [(i, s) for i, s in cases if name in {seg_name(x) for x in Fingerprint.of(s.get("fingerprint")).get(field[kind])}]   # 兩邊都去數字再比
        if not hit:
            continue
        hit.sort(key=lambda x: x[1].get("start") or "", reverse=True)
        out.append({"segment": seg, "kind": kind, "evidence": evidence, "order": order,
                    "cases": [{"id": i, "title": s.get("title"), "start": s.get("start"), "category": s.get("root_cause_category")} for i, s in hit]})
    out.sort(key=lambda h: (-len(h["cases"]), h["order"]))
    return out[:k]


def checklist(category: str | None, cases: list[tuple[str, dict]], defaults: dict | None = None, k: int = 5) -> list[dict]:
    """這一類根因「先查什麼」：同一類、已確認的事故裡老師傅勾過的（checked）依次數排前面，不夠 k 項用情境資料檔的
    domain.checklists 補。回傳 [{item, n（過去勾過幾次；0＝設定檔的預設）}]。沒有分類就空。"""
    if not category:
        return []
    counts: dict[str, int] = {}
    for _, s in cases:
        if s.get("root_cause_category") == category:
            for it in s.get("checked") or []:
                counts[it] = counts.get(it, 0) + 1
    items = [{"item": it, "n": n} for it, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    for it in (defaults or {}).get(category) or []:
        if it not in counts:
            items.append({"item": it, "n": 0})
    return items[:k]


def format_hints(hints: list[dict], todo: list[dict], cls: dict | None) -> list[str]:
    """報告最前面、分類之後：線索（當下已證實、過去也出事過的段）與這一類的先查清單。都沒有就空。"""
    lines = []
    if hints:
        lines += ["## 歷史 case 的線索（只列這次資料已經看到變化的段）", ""]
        for i, h in enumerate(hints, 1):
            what = "變慢" if h["kind"] == "slow" else "錯誤源頭"
            past = "、".join(f"{(c['start'] or '')[:10]} {c['title'] or c['id'][:12]}" for c in h["cases"][:3])
            lines.append(f"{i}. **{h['segment']}**（這次{what}：{h['evidence']}）：過去 {len(h['cases'])} 件確認過的事故也是這段（{past}）")
        lines += ["", "> 排序只代表「先看哪個」；是不是它，看上面這次的數字。", ""]
    if todo and cls:
        lines += [f"## 像 {cls['category']} 類：先查什麼", ""]
        lines += [f"- [ ] {t['item']}" + (f"（過去 {t['n']} 件有查）" if t["n"] else "") for t in todo]
        lines.append("")
    return lines

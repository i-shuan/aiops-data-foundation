"""資料品質檢查：餵給 baseline 之前，先看資料本身怪不怪（configs/baseline/README.md「資料品質檢查」）。

為什麼要：baseline 用中位數＋MAD，只擋得住「少數」怪的點；資料本身壞掉時（路由沒參數化、欄位缺、資料停了），
學出來的「平常」就是錯的，或根本學不到。這幾種壞法不用學、不用歷史，用規則就看得出來。

每個檢查（configs/baseline/quality.yaml）看最近 window 這段時間，跟 ref_offset 之前（預設 1 天前，同一個時段）比：

  type: cardinality  某個欄位的不同值有幾個（例如每個服務有幾條路由）。暴增多半是路由沒參數化：
                     /orders/123 沒換成 /orders/{id}，每個訂單一條序列，序列數爆炸、每條都學不到。
                     另外看抽樣的值裡有幾成「長得像帶了 ID」（數字、UUID、長 hex）。
  type: missing      必填欄位缺了幾成（例如 service.name、k8s.pod.name）。缺的資料對不到服務，學不進任何序列。
  type: stopped      之前有資料、最近 silent 這段時間完全沒有（服務、或整個來源停了）。不是 0 流量，是收不到。
  type: share        某一部分佔全部的比例變多（例如落到 unassigned 的 log）：dept 清單漏同步、namespace 沒標 dept。
  type: lag          兩個時間欄位差多少（依 by 分組，看百分位）：
                     收資料延遲 event.created（Collector 讀到）→ event.ingested（寫進 ES）：Collector 卡住、ES 慢時，
                     最新幾分鐘還沒進來，看起來像流量掉；
                     應用時鐘偏差 @timestamp（應用自己寫的）→ event.created：應用主機時鐘偏了、或時區寫錯。

eval_* 只做計算，不碰 ES（單元測試用）；fetch_* 是查詢。
"""
from __future__ import annotations

import re

import pandas as pd

from core.es import ES, time_range

# 路徑裡「像是 ID」的一段：純數字、UUID、8 碼以上的 hex、混了數字的長字串（例如訂單號 A20261023001）
_ID_SEGMENT = re.compile(
    r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{8,}|"
    r"(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{10,})$")


def looks_like_id(value: str) -> bool:
    """這個值裡有沒有一段像是 ID（沒參數化的路由）。樣板（{id}、:id）不算。"""
    for seg in re.split(r"[/?=&]", str(value)):
        if seg and not seg.startswith(("{", ":")) and _ID_SEGMENT.match(seg):
            return True
    return False


def id_share(values: list[str]) -> float:
    return sum(looks_like_id(v) for v in values) / len(values) if values else 0.0


def _ref_of(ref: pd.DataFrame, keys: list[str], col: str) -> dict:
    if ref is None or ref.empty:
        return {}
    return {tuple(r[k] for k in keys): r[col] for r in ref.to_dict("records")}


def eval_cardinality(now: pd.DataFrame, ref: pd.DataFrame | None, keys: list[str], *, max_values: int | None = None,
                     ratio: float = 3.0, min_increase: int = 50, id_share_min: float = 0.5) -> list[dict]:
    """now、ref：keys + ["values"]（不同值的個數）；now 另有 "examples"（抽樣的值）。

    觸發（任一）：
      (1) values ≥ max_values（有設才看）
      (2) 跟 ref 比 ≥ ratio 倍，而且多了 ≥ min_increase 個（ref 沒有這個對象時，當成從 0 開始：新服務）
      (3) 抽樣的值裡 ≥ id_share_min 像帶了 ID，而且 values ≥ min_increase（少數幾個不算）
    ref 整段沒有資料（第一天、ref 那段資料已刪）：沒有比較對象，(2) 不看，只看 (1)、(3)。
    不然第一天每個路由 ≥ min_increase 條的正常服務都會報「從 0 個變 N 個」。
    """
    first_day = ref is None or ref.empty
    before = _ref_of(ref, keys, "values")
    hits = []
    for r in now.to_dict("records"):
        k = tuple(r[f] for f in keys)
        n, b = int(r["values"]), before.get(k)
        share = id_share(r.get("examples") or [])
        why = []
        if max_values is not None and n >= max_values:
            why.append(f"{n} 個，超過上限 {max_values}")
        if not first_day and n - (b or 0) >= min_increase and (b is None or b == 0 or n >= ratio * b):
            why.append(f"從 {b or 0} 個變 {n} 個")
        if share >= id_share_min and n >= min_increase:
            why.append(f"抽樣的值 {share:.0%} 像帶了 ID")
        if why:
            hits.append({"key": dict(zip(keys, k)), "value": n, "ref": b, "id_share": share,
                         "examples": [v for v in (r.get("examples") or []) if looks_like_id(v)][:3], "why": why})
    return hits


def eval_missing(now: pd.DataFrame, ref: pd.DataFrame | None, keys: list[str], fields: list[str], *,
                 min_docs: int = 100, max_missing: float | None = 0.05, max_increase: float | None = None) -> list[dict]:
    """now、ref：keys + ["docs"] + 每個欄位一欄 "missing:<欄位>"（缺的筆數）。

    每個對象、每個欄位：缺的比例 ≥ max_missing（有設才看），或比 ref 多了 ≥ max_increase（有設、ref 也有量才看）。
    筆數 < min_docs 的不看（幾筆缺一筆就是好幾成）。
    """
    ref_rows = {} if ref is None or ref.empty else {tuple(r[k] for k in keys): r for r in ref.to_dict("records")}
    hits = []
    for r in now.to_dict("records"):
        if r["docs"] < min_docs:
            continue
        k = tuple(r[f] for f in keys)
        for f in fields:
            share = r[f"missing:{f}"] / r["docs"]
            rr = ref_rows.get(k)
            b = rr[f"missing:{f}"] / rr["docs"] if rr is not None and rr["docs"] >= min_docs else None
            why = []
            if max_missing is not None and share >= max_missing:
                why.append(f"{share:.0%} 缺 {f}")
            if max_increase is not None and b is not None and share - b >= max_increase:
                why.append(f"缺 {f} 的比例從 {b:.0%} 變 {share:.0%}")
            if why:
                hits.append({"key": dict(zip(keys, k)), "field": f, "value": share, "ref": b, "docs": int(r["docs"]),
                             "why": why})
    return hits


def eval_stopped(before: pd.DataFrame, recent: pd.DataFrame | None, keys: list[str], *, min_docs: int = 10) -> list[dict]:
    """before：keys + ["docs"]，之前那段時間的筆數；recent：最近 silent 這段時間有資料的對象（keys + ["docs"]）。

    之前有 ≥ min_docs 筆、最近一筆都沒有的才算（之前就很少的，最近剛好沒有不奇怪）。
    """
    alive = set() if recent is None or recent.empty else {tuple(r[k] for k in keys) for r in recent.to_dict("records")
                                                           if r["docs"] > 0}
    return [{"key": dict(zip(keys, k)), "value": 0, "ref": int(r["docs"]), "why": [f"之前 {int(r['docs'])} 筆，最近沒有"]}
            for r in before.to_dict("records")
            if r["docs"] >= min_docs and (k := tuple(r[f] for f in keys)) not in alive]


def eval_present(now: pd.DataFrame, keys: list[str], *, min_docs: int = 1) -> list[dict]:
    """now：keys + ["docs"]。有 ≥ min_docs 筆就報：這個檢查的 filter 是「不該出現的資料」（例如 Collector 沒改名的舊指標名字）。"""
    return [{"key": dict(zip(keys, (r[k] for k in keys))), "value": int(r["docs"]), "why": [f"最近有 {int(r['docs'])} 筆"]}
            for r in now.to_dict("records") if r["docs"] >= min_docs]


def eval_share(part: int, total: int, ref_part: int | None, ref_total: int | None, *, min_docs: int = 100,
               max_share: float | None = None, min_increase: float = 0.1) -> list[dict]:
    """part／total：最近這段時間「那一部分」和全部的筆數；ref_*：ref 那段時間的。

    觸發（任一）：比例 ≥ max_share（有設才看）；比 ref 多了 ≥ min_increase（ref 也有量才看）。全部 < min_docs 不看。
    """
    if total < min_docs:
        return []
    share = part / total
    b = ref_part / ref_total if ref_total and ref_total >= min_docs and ref_part is not None else None
    why = []
    if max_share is not None and share >= max_share:
        why.append(f"佔 {share:.0%}，超過 {max_share:.0%}")
    if b is not None and share - b >= min_increase:
        why.append(f"從 {b:.0%} 變 {share:.0%}")
    return [{"key": {}, "value": share, "ref": b, "part": part, "total": total, "why": why}] if why else []


def eval_lag(df: pd.DataFrame, keys: list[str], *, stat: str = "p95", max_seconds: float = 60, min_docs: int = 50,
             absolute: bool = False) -> list[dict]:
    """df：keys + ["docs", "p50", "p95"]（秒，第二個欄位減第一個）。stat 的值 ≥ max_seconds 就報。
    absolute：兩個方向都算（時鐘偏差：應用的時間比節點快或慢都不對）。筆數 < min_docs 的不看。"""
    hits = []
    for r in df.to_dict("records"):
        v = r.get(stat)
        if r["docs"] < min_docs or v is None or v != v:
            continue
        if (abs(v) if absolute else v) >= max_seconds:
            hits.append({"key": {k: r[k] for k in keys}, "value": float(v), "ref": None, "docs": int(r["docs"]),
                         "why": [f"{stat} {v:+.0f} 秒（{int(r['docs'])} 筆）" if absolute else f"{stat} {v:.0f} 秒（{int(r['docs'])} 筆）"]})
    return hits


def describe(check: dict, hit: dict) -> str:
    # 缺欄位的那組（missing_bucket）寫「沒有 namespace」，不寫「全部」：全部是沒有分組的檢查（例如 share）
    who = "、".join(f"{v}" if v is not None and v == v else f"沒有 {k}" for k, v in hit["key"].items()) or "全部"
    msg = f"{check.get('title', check['name'])}：{who}，{'；'.join(hit['why'])}"
    if hit.get("examples"):
        msg += f"（例如 {', '.join(hit['examples'])}）"
    return msg


# ── 查詢 ──

def _query(check: dict, start, end) -> dict:
    # time_field：用哪個時間篩「最近這段」。預設 @timestamp；時鐘偏差、收資料延遲的檢查不能用 @timestamp，
    # 因為那種資料的 @timestamp 本身就錯了（8 小時前、在未來）或很舊（卡住的），會被篩掉
    f = [time_range(start, end, check.get("time_field", "@timestamp"))]
    if check.get("filter"):
        f.append(check["filter"])
    return {"bool": {"filter": f}}


def _composite(es: ES, index: str, query: dict, keys: list[str], fields: list[str], aggs: dict,
               size: int = 1000) -> list[dict]:
    """依 fields 分組（composite，自動翻頁），每組一列：keys + 每個子聚合 + docs。"""
    sources = [{k: {"terms": {"field": f, "missing_bucket": True}}} for k, f in zip(keys, fields)]
    rows, after = [], None
    while True:
        comp = {"size": size, "sources": sources, **({"after": after} if after else {})}
        body = {"size": 0, "query": query, "aggs": {"g": {"composite": comp, "aggs": aggs}}}
        g = es.search(index, body).get("aggregations", {}).get("g", {})
        for b in g.get("buckets", []):
            rows.append({**b["key"], "docs": b["doc_count"], **{a: b[a] for a in aggs}})
        after = g.get("after_key")
        if not after or not g.get("buckets"):
            return rows


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    """composite 的結果 → DataFrame。dtype=object：缺欄位的那組（missing_bucket）的 key 是 None，
    pandas 3 的字串欄位會把 None 變成 NaN，寫進 aiops-anomalies 的 keys 變成 JSON 的 NaN，ES 整批拒收、quality 指令中斷
    （這一輪所有檢查的結果都沒寫進去），訊息裡也印成 nan。"""
    return pd.DataFrame(rows, columns=columns, dtype=object)


def _keys(check: dict) -> list[str]:
    return check.get("keys") or [f.split(".")[-1] for f in check.get("by", [])]


def fetch_cardinality(es: ES, check: dict, start, end, examples: int = 500) -> pd.DataFrame:
    """examples：每組抽幾個值（看像不像帶了 ID）；ref 那段只要個數，給 0。
    terms 依筆數排前面的，所以要抽到夠多：像 ID 的值每個只有幾筆，抽太少只抽到量大的樣板路由，比例被低估
    （40 條樣板＋45 條像 ID、只抽 50 個時只看到 20%）。不同值 ≤ examples 時等於全看；更多的話 max_values 會先報。"""
    keys = _keys(check)
    aggs = {"n": {"cardinality": {"field": check["field"], "precision_threshold": 3000}}}
    if examples:
        aggs["ex"] = {"terms": {"field": check["field"], "size": examples}}
    # 一頁的組數 × 每組的值不能超過 ES 的 search.max_buckets（預設 65,536）
    size = max(10, min(1000, 60000 // (examples + 2)))
    rows = _composite(es, check["index"], _query(check, start, end), keys, check.get("by", []), aggs, size)
    return _frame([{**{k: r[k] for k in keys}, "values": r["n"]["value"],
                          "examples": [b["key"] for b in r["ex"]["buckets"]] if examples else []} for r in rows],
                        columns=keys + ["values", "examples"])


def fetch_missing(es: ES, check: dict, start, end) -> pd.DataFrame:
    keys = _keys(check)
    aggs = {f"missing:{f}": {"filter": {"bool": {"must_not": {"exists": {"field": f}}}}} for f in check["fields"]}
    rows = _composite(es, check["index"], _query(check, start, end), keys, check.get("by", []), aggs)
    return _frame([{**{k: r[k] for k in keys}, "docs": r["docs"],
                          **{a: r[a]["doc_count"] for a in aggs}} for r in rows],
                        columns=keys + ["docs"] + list(aggs))


def fetch_counts(es: ES, check: dict, start, end) -> pd.DataFrame:
    keys = _keys(check)
    rows = _composite(es, check["index"], _query(check, start, end), keys, check.get("by", []), {})
    return _frame([{**{k: r[k] for k in keys}, "docs": r["docs"]} for r in rows], columns=keys + ["docs"])


def count(es: ES, index: str, check: dict, start, end) -> int:
    body = {"size": 0, "track_total_hits": True, "query": _query(check, start, end)}
    return int(es.search(index, body)["hits"]["total"]["value"])


def fetch_lag(es: ES, check: dict, start, end) -> pd.DataFrame:
    """每組的時間差百分位（秒）：check 的 to 欄位減 from 欄位。兩個欄位都要有。"""
    keys = _keys(check)
    q = _query(check, start, end)
    q["bool"]["filter"] += [{"exists": {"field": check["from"]}}, {"exists": {"field": check["to"]}}]
    script = {"source": "doc[params.t].value.toInstant().toEpochMilli() - doc[params.f].value.toInstant().toEpochMilli()",
              "params": {"f": check["from"], "t": check["to"]}}
    aggs = {"lag": {"percentiles": {"script": script, "percents": [50, 95]}}}
    rows = _composite(es, check["index"], q, keys, check.get("by", []), aggs)
    out = []
    for r in rows:
        v = r["lag"]["values"]
        ms = lambda k: v.get(k) if v.get(k) is not None else float("nan")
        out.append({**{k: r[k] for k in keys}, "docs": r["docs"], "p50": ms("50.0") / 1000, "p95": ms("95.0") / 1000})
    return _frame(out, columns=keys + ["docs", "p50", "p95"])

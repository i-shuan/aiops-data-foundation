"""資料品質檢查：餵給 baseline 之前，先看資料本身怪不怪（configs/baseline/reference.md「資料品質檢查」）。

為什麼要：baseline 用中位數＋MAD，只擋得住「少數」怪的點；資料本身壞掉時（路由沒參數化、欄位缺、資料停了），
學出來的「平常」就是錯的，或根本學不到。這幾種壞法不用學、不用歷史，用規則就看得出來。

每個檢查（configs/baseline/quality.yaml）看最近 window 這段時間，跟 ref_offset 之前（預設 1 天前，同一個時段）比：

  type: cardinality  某個欄位的不同值有幾個（例如每個服務有幾條路由）。暴增多半是路由沒參數化：
                     /orders/123 沒換成 /orders/{id}，每個訂單一條序列，序列數爆炸、每條都學不到。
                     另外看抽樣的值裡有幾成「長得像帶了 ID」（數字、UUID、長 hex）。
  type: missing      必填欄位缺了幾成（例如 service.name、k8s.pod.name）。缺的資料對不到服務，學不進任何序列。
                     missing_if_equals：欄位等於另一個欄位也算缺（service.name 被 Collector 補成 container 名稱）。
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

from detect.store.quality_repo import check_keys  # noqa: F401  讀統計在 repository；keys 的算法兩邊共用
from detect.store.models import Anomaly
from detect.base import Detector
from detect.utils import _freq, _v
from detect.config.quality import QualityCheck, QualityConfig

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
        if r["docs"] < min_docs or pd.isna(v):   # None、NaN 都跳過
            continue
        if (abs(v) if absolute else v) >= max_seconds:
            hits.append({"key": {k: r[k] for k in keys}, "value": float(v), "ref": None, "docs": int(r["docs"]),
                         "why": [f"{stat} {v:+.0f} 秒（{int(r['docs'])} 筆）" if absolute else f"{stat} {v:.0f} 秒（{int(r['docs'])} 筆）"]})
    return hits


def describe(check: QualityCheck, hit: dict) -> str:
    # 缺欄位的那組（missing_bucket）寫「沒有 namespace」，不寫「全部」：全部是沒有分組的檢查（例如 share）
    who = "、".join(f"沒有 {k}" if pd.isna(v) else f"{v}" for k, v in hit["key"].items()) or "全部"
    msg = f"{check.title or check.name}：{who}，{'；'.join(hit['why'])}"
    if hit.get("examples"):
        msg += f"（例如 {', '.join(hit['examples'])}）"
    return msg


def _or(value, default):
    """沒寫（None）才用預設；寫 0 就是 0。每種檢查（type）的預設不一樣（min_docs：cardinality 100、stopped 10、present 1、lag 50），所以預設留在這裡，不放進模型。"""
    return default if value is None else value


def quality_hits(q, c: QualityCheck, now, window, ref_offset) -> list[dict]:
    """跑一個檢查，回傳觸發的對象（eval_* 的結果）。q 是 repo.quality（讀統計）。"""
    start, rs, re_ = now - window, now - window - ref_offset, now - ref_offset
    keys, t = check_keys(c), c.type
    if t == "cardinality":
        return eval_cardinality(q.cardinality(c, start, now, _or(c.sample_values, 500)), q.cardinality(c, rs, re_, 0), keys,
                                  max_values=c.max_values, ratio=_or(c.ratio, 3.0),
                                  min_increase=_or(c.min_increase, 50), id_share_min=_or(c.id_share, 0.5))
    if t == "missing":
        return eval_missing(q.missing(c, start, now), q.missing(c, rs, re_), keys, c.fields,
                              min_docs=_or(c.min_docs, 100), max_missing=c.max_missing,
                              max_increase=c.max_increase)
    if t == "stopped":
        silent = pd.Timedelta(_freq(_or(c.silent, "10m")))
        lookback = pd.Timedelta(_freq(_or(c.lookback, "1h")))
        return eval_stopped(q.counts(c, now - lookback, now - silent), q.counts(c, now - silent, now),
                              keys, min_docs=_or(c.min_docs, 10))
    if t == "present":
        return eval_present(q.counts(c, start, now), keys, min_docs=_or(c.min_docs, 1))
    if t == "share":
        return eval_share(q.count(c.part_index, c, start, now), q.count(c.index, c, start, now),
                            q.count(c.part_index, c, rs, re_), q.count(c.index, c, rs, re_),
                            min_docs=_or(c.min_docs, 100), max_share=c.max_share,
                            min_increase=_or(c.min_increase, 0.1))
    if t == "lag":
        return eval_lag(q.lag(c, start, now), keys, stat=_or(c.stat, "p95"),
                          max_seconds=_or(c.max_seconds, 60), min_docs=_or(c.min_docs, 50),
                          absolute=_or(c.absolute, False))
    raise ValueError(f"不認得的檢查種類：{t}")


class QualityDetector(Detector):
    level = "quality"
    interval_default = "10m"

    def checks(self) -> list[QualityCheck]:
        self.ref_offset = pd.Timedelta(_freq(self.config.ref_offset))
        return self.config.checks

    def evaluate(self, c: QualityCheck) -> list[Anomaly]:
        window = pd.Timedelta(_freq(c.window or self.config.window))
        out = []
        for h in quality_hits(self.repo.quality, c, self.now, window, self.ref_offset):
            keys = {**{k: v for k, v in h["key"].items() if _v(v) is not None},   # NaN 寫進 ES 會整批被拒
                    **({"field": h["field"]} if "field" in h else {})}
            out.append(Anomaly(ts=self.slot, series=c.name, rule=c.name, keys=keys, value=h["value"],
                               median=_v(h.get("ref")), level=self.level, direction="up",
                               severity=_or(c.severity, "warning"), message=describe(c, h)))
        return out


def cmd_quality(quality_config: QualityConfig, args, repo=None) -> int:
    return QualityDetector(quality_config, args, repo).run()

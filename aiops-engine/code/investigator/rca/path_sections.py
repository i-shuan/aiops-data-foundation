"""根因分析報告裡「有 path」才有的四段（path＝把一個單位串起來的那份資料：IT 的 trace、工廠的履歷；兩者存成同一種合成格式，
sources/traces.merged_doc）。plugin 的 report_sections() 把這裡的 SECTIONS 加進報告（plugins/observability、plugins/factory）；
只有指標的情境（metrics_only）不加。框架與中立四段在 report.py。

  entries     這個對象收到的單位數、p95、趨勢；順便取樣慢的單位與平常的單位（後面幾段用）。沒有 path 時只記一行，後面要 path 的段各自略過
  segments    慢在哪一段（M4：每段自己花的時間跟平常比）＋錯誤最早出在哪一段＋慢的那批單位的共同點（commonality）
  slowest     最慢那個單位的瀑布圖
  topology    這些單位經過的對象（mermaid）

entries 要排在 logs 前面：它挑「平常」是哪一段（看那段有沒有單位）、抽樣本，logs 與結論用它的結果。
"""
from __future__ import annotations

from typing import Callable

from core import plugin
from sources import spans_of
from sources import traces as T

from . import analyze as A
from . import queries as E
from .report import PAD, Ctx, _delta, _dur, _ms, _skip, pick_reference


def sec_entries(c: Ctx) -> None:
    """這個對象收到的單位：數量、錯誤、p50／p95 跟平常比；出事前的趨勢；取樣最慢的那批與平常的那批。"""
    es, ix, U, PATH = c.es, c.ix, c.U, c.PATH
    pick_reference(c, probe=lambda rs, re_: bool(E.entry_stats(es, ix["traces"], c.entity, c.where, rs, re_, c.lag)["requests"]))
    now = E.entry_stats(es, ix["traces"], c.entity, c.where, c.start, c.end, c.lag)
    c.heading(f"{PATH}：這個{plugin.entity_label()}收到的{U}")
    c.findings["requests"] = now
    if not now["requests"]:
        c.has_paths = False
        c.out += [f"這段時間沒有這個{plugin.entity_label()}的{PATH}（沒埋點、名稱不對、或{PATH}還沒合成完；合成晚 1～2 分鐘）。"
                  f"先從 log 找；沒有{PATH}的件系統會自動重跑報告（每 2 分鐘、最多 5 次）。", ""]
        return
    c.has_paths = True
    ref = E.entry_stats(es, ix["traces"], c.entity, c.where, c.ref_start, c.ref_end, c.lag) if c.ref_start else None
    c.out += [f"| | 現在 | 平常（{c.ref_label or '沒有資料'}） |", "|---|---|---|"]
    for k, label in (("requests", f"{U}數（{PATH}，有取樣）"), ("errors", "錯誤"), ("p50_ms", "p50"), ("p95_ms", "p95")):
        f = _ms if k.endswith("_ms") else str
        c.out.append(f"| {label} | {f(now[k])} | {f(ref[k]) if ref else '—'} |")
    c.out.append("")
    # 出事前的趨勢：前面幾段（預設 6 段、每段 10 分鐘）的 p95，一路往上＝慢慢惡化、平的然後跳＝突然
    tcfg = c.cfg.get("trend") or {}
    tw, tn = _dur(str(tcfg.get("window", "10m"))), int(tcfg.get("windows", 6))
    p95s = [E.entry_stats(es, ix["traces"], c.entity, c.where, c.start - tw * (k + 1), c.start - tw * k, c.lag)["p95_ms"] for k in reversed(range(tn))]
    c.trend = A.classify_trend(p95s, now["p95_ms"])
    c.trend["window"], c.trend["windows"] = str(tcfg.get("window", "10m")), tn
    c.findings["trend"] = c.trend
    c.period_label = f"{int(tw.total_seconds() * tn // 60)} 分鐘"
    c.out += [A.describe_trend(c.trend, c.period_label, _ms), ""]
    now_ids = E.sample_path_ids(es, ix["traces"], c.entity, c.where, c.start, c.end, c.n, slowest=True, lag=c.lag)
    c.now_tr = T.group_traces(spans_of(es, ix["traces"], now_ids, c.start - PAD - c.lag, c.end + PAD))
    if c.ref_start:
        ref_ids = E.sample_path_ids(es, ix["traces"], c.entity, c.where, c.ref_start, c.ref_end, c.n, slowest=False, lag=c.lag)
        c.ref_tr = T.group_traces(spans_of(es, ix["traces"], ref_ids, c.ref_start - PAD - c.lag, c.ref_end + PAD))


def sec_segments(c: Ctx) -> None:
    """慢在哪一段（每段自己花的時間跟平常比）、錯誤最早出在哪一段、慢的那批的共同點。"""
    U, PATH = c.U, c.PATH
    if not c.pathed:
        return _skip(c, f"{PATH}：慢在哪一段", f"（沒有{PATH}，略過）")
    diffs = A.compare_segments(c.now_tr, c.ref_tr)
    c.heading(f"{PATH}：慢在哪一段")
    c.out += [f"比較最慢、出錯的 {len(c.now_tr)} 個{U}和平常的 {len(c.ref_tr)} 個{U.rstrip()}，每個{U}在每一段**自己**花的時間"
              "（扣掉等下游的時間，所以慢的那段不會一路算到最上層）：", "",
              f"| 段 | 現在 | 平常 | 多了 | 每個{U}呼叫幾次 |", "|---|---|---|---|---|"]
    for d in diffs[:6]:
        c.out.append(f"| {d.segment} | {_ms(d.now_ms)} | {_ms(d.ref_ms)} | **{_delta(d.delta_ms)}** "
                     f"| {d.now_calls:.1f}（平常 {d.ref_calls:.1f}） |")
    errs = sorted([d for d in diffs if d.now_errors > d.ref_errors], key=lambda d: d.now_errors - d.ref_errors, reverse=True)
    c.diffs, c.errs, c.top = diffs, errs, [d for d in diffs if d.delta_ms > 0][:3]
    c.findings["segments"] = [{"segment": d.segment, "now_ms": d.now_ms, "ref_ms": d.ref_ms, "delta_ms": d.delta_ms,
                               "now_calls": d.now_calls, "ref_calls": d.ref_calls} for d in diffs[:6]]
    c.findings["error_origins"] = [{"segment": d.segment, "now_errors": d.now_errors, "ref_errors": d.ref_errors} for d in errs[:5]]
    if errs:
        c.out += ["", "錯誤最早出在哪一段（上層只是把錯誤往上傳）：", "", "| 段 | 現在 | 平常 |", "|---|---|---|"]
        c.out += [f"| {d.segment} | {d.now_errors} 個{U} | {d.ref_errors} |" for d in errs[:5]]
    c.out.append("")
    # 慢的那批有什麼共同點（平常沒這麼集中）
    ccfg = c.cfg.get("commonality") or {}
    commons = A.commonality(c.now_tr, c.ref_tr, res_keys=tuple(ccfg.get("resource") or ("k8s.pod.name", "service.version", "k8s.node.name")),
                            attr_keys=tuple(ccfg.get("attrs") or ("lot.id", "db.statement")),
                            min_share=float(ccfg.get("min_share", 0.6)), min_lift=float(ccfg.get("min_lift", 2)),
                            min_paths=int(ccfg.get("min_traces", 5)))
    c.commons = commons
    c.findings["commonality"] = [{"dim": x.dim, "value": x.value, "now": x.now, "now_share": x.now_share, "ref_share": x.ref_share}
                                 for x in commons[:5]]
    if commons:
        c.out += [f"這批慢的{U}有什麼共同點（平常沒這麼集中）：", "", f"| 看什麼 | 值 | 慢的{U}裡 | 平常 |", "|---|---|---|---|"]
        c.out += [f"| {x.dim} | `{x.value}` | {x.now_share:.0%}（{x.now} 個） | {'平常沒見過（新的？）' if x.is_new else f'{x.ref_share:.0%}'} |"
                  for x in commons[:5]]
        c.out.append("")
    c.suspects = list(dict.fromkeys([d.segment.split(" › ")[0] for d in c.top + errs[:3]] + [c.entity]))


def sec_slowest(c: Ctx) -> None:
    U = c.U
    sl = c.slowest()
    if not sl:
        return _skip(c, f"最慢的那個{U.strip()}", f"（沒有{c.PATH}，略過）")
    idl = "lot" if U.strip() == "lot" else "trace_id"
    c.heading(f"最慢的那個{U}（{idl} `{sl[0]['trace_id']}`）")
    c.out += ["```"] + A.waterfall(sl) + ["```", ""]


def sec_topology(c: Ctx) -> None:
    if not c.pathed:
        return _skip(c, f"這些{c.U}經過的{plugin.entity_label()}", f"（沒有{c.PATH}，略過）")
    edges = A.topology(c.now_tr)
    c.findings["topology"] = [{"caller": e.caller, "callee": e.callee, "calls": e.calls, "errors": e.errors} for e in edges]
    c.heading(f"這些{c.U}經過的{plugin.entity_label()}")
    c.out += ["```mermaid", A.mermaid(edges), "```", ""]


SECTIONS: dict[str, Callable[[Ctx], None]] = {
    "entries": sec_entries, "segments": sec_segments, "slowest": sec_slowest, "topology": sec_topology,
}

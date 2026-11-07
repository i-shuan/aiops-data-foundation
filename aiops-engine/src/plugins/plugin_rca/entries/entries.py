"""entries：這個服務收到的請求跟平常比（報告的一段；原本是 incident/rca_report.py 的 sec_entries）。
報告的骨架（Ctx、標題編號、共用的小工具）在 incident/rca_report.py；這個外掛只寫這一段。"""
from __future__ import annotations

from common.clock import parse_duration
import data.store.models as T
import incident.rca_analyzer as A
from incident.rca_report import Ctx, PAD, _ms


def run(c: Ctx) -> None:
    """這個對象收到的單位：數量、錯誤、p50／p95 跟平常比；出事前的趨勢；取樣最慢的那批與平常的那批。"""
    tr, ix, U, TRACE = c.repo.traces, c.ix, c.U, c.TRACE
    now = tr.entry_stats(ix.traces, c.service, c.route, c.start, c.end, c.lag)
    c.heading(f"{TRACE}：這個{c.sc.entity}收到的{U}")
    c.findings["requests"] = now
    if not now["requests"]:
        c.has_traces = False
        c.out += [f"這段時間沒有這個{c.sc.entity}的{TRACE}（沒埋點、名稱不對、或{TRACE}還沒合成完；合成晚 1～2 分鐘）。"
                  f"先從 log 找；沒有{TRACE}的件系統會自動重跑報告（每 2 分鐘、最多 5 次）。", ""]
        return
    c.has_traces = True
    ref = tr.entry_stats(ix.traces, c.service, c.route, c.ref_start, c.ref_end, c.lag) if c.ref_start else None
    c.out += [f"| | 現在 | 平常（{c.ref_label or '沒有資料'}） |", "|---|---|---|"]
    for k, label in (("requests", f"{U}數（{TRACE}，有取樣）"), ("errors", "錯誤"), ("p50_ms", "p50"), ("p95_ms", "p95")):
        f = _ms if k.endswith("_ms") else str
        c.out.append(f"| {label} | {f(now[k])} | {f(ref[k]) if ref else '—'} |")
    c.out.append("")
    # 出事前的趨勢：前面幾段（預設 6 段、每段 10 分鐘）的 p95，一路往上＝慢慢惡化、平的然後跳＝突然
    trend_config = c.rca_config.trend
    tw, tn = parse_duration(trend_config.window), trend_config.windows
    p95s = [tr.entry_stats(ix.traces, c.service, c.route, c.start - tw * (k + 1), c.start - tw * k, c.lag)["p95_ms"] for k in reversed(range(tn))]
    c.trend = A.classify_trend(p95s, now["p95_ms"])
    c.trend["window"], c.trend["windows"] = trend_config.window, tn
    c.findings["trend"] = c.trend
    c.span_label = f"{int(tw.total_seconds() * tn // 60)} 分鐘"
    c.out += [A.describe_trend(c.trend, c.span_label, _ms), ""]
    now_ids = tr.sample_trace_ids(ix.traces, c.service, c.route, c.start, c.end, c.n, slowest=True, lag=c.lag)
    c.now_tr = T.group_traces(tr.spans(ix.traces, now_ids, c.start - PAD - c.lag, c.end + PAD))
    if c.ref_start:
        ref_ids = tr.sample_trace_ids(ix.traces, c.service, c.route, c.ref_start, c.ref_end, c.n, slowest=False, lag=c.lag)
        c.ref_tr = T.group_traces(tr.spans(ix.traces, ref_ids, c.ref_start - PAD - c.lag, c.ref_end + PAD))

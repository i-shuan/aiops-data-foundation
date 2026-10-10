"""用法（在 aiops 底下）：

  # 從 baseline 找到的異常開始（最近 30 分鐘裡最嚴重的那個服務）
  python3 -m aiops_rca investigate --config rca/rca.yaml
  # 指定服務、API、時間
  python3 -m aiops_rca investigate --config rca/rca.yaml --service mes-api --route "GET /lots/{id}" \\
      --start 2026-10-10T02:00:00Z --end 2026-10-10T02:15:00Z
  # 服務之間的呼叫關係
  python3 -m aiops_rca topology --config rca/rca.yaml --window 1h

investigate 照「Metrics 發現異常 → Trace 縮小到哪一段 → Log 找出原因」的順序查，印出一份 Markdown 報告；
--out 另存成檔案。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone

import yaml

from aiops_common import traces as T

from . import analyze as A
from . import es as E


def _dur(s: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([smhdw])", s)
    if not m:
        raise SystemExit(f"時間長度看不懂：{s}（例如 30m、1h、7d）")
    n, u = int(m[1]), m[2]
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[u]: n})


def _ts(s: str | None) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else datetime.now(timezone.utc)


def _local(t: datetime, tz: str) -> str:
    from zoneinfo import ZoneInfo
    return t.astimezone(ZoneInfo(tz)).strftime("%m-%d %H:%M")


def _ms(v: float) -> str:
    return f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms" if v >= 10 else f"{v:.1f} ms"


def _delta(v: float) -> str:
    return ("+" if v >= 0 else "-") + _ms(abs(v))


def _svc_of(keys: dict) -> str | None:
    """異常是哪個服務：metrics 的序列用 resource.attributes.service.name，trace 每一段的用 entry.service。"""
    return keys.get("resource.attributes.service.name") or keys.get("entry.service")


def _route_of(keys: dict) -> str | None:
    """哪支 API（trace 裡 Server span 的名稱，例如 "GET /lots/{id}"）。"""
    if keys.get("entry.name"):
        return keys["entry.name"]
    if keys.get("attributes.http_request_method") and keys.get("attributes.http_route"):
        return f"{keys['attributes.http_request_method']} {keys['attributes.http_route']}"
    return None


def pick_from_anomalies(es, cfg, start, end):
    """沒指定服務：從 baseline 寫的異常裡，挑這段時間最嚴重的服務（偏離倍數最大），時間範圍取它異常的那段。
    異常帶著 method、API 的話（http.* 序列），也只查那支 API。"""
    rows = E.anomalies(es, cfg["indices"]["anomalies"], start, end)
    rows = [r for r in rows if _svc_of(r.get("keys") or {})]
    if not rows:
        return None
    top = max(rows, key=lambda r: abs(r.get("z") or 0))
    svc, route = _svc_of(top["keys"]), _route_of(top["keys"])
    mine = [r for r in rows if _svc_of(r["keys"]) == svc]
    t = sorted(_ts(r["@timestamp"]) for r in mine)
    # 異常的時間是那 5 分鐘的開始，所以查到最後一個異常再往後 5 分鐘
    return svc, route, t[0], t[-1] + _dur(cfg.get("interval", "5m")), mine


def investigate(cfg: dict, args) -> str:
    es = E.ES()
    ix = cfg["indices"]
    tz = cfg.get("timezone", "Asia/Taipei")
    n = cfg.get("sample", 200)
    service, route, found = args.service, args.route, []
    if args.start:
        start, end = _ts(args.start), _ts(args.end)
    else:
        end = _ts(args.at)
        start = end - _dur(args.window)
    if not service:
        picked = pick_from_anomalies(es, cfg, start, end)
        if not picked:
            return f"{_local(start, tz)}～{_local(end, tz)} 沒有 baseline 找到的異常。要查特定服務的話用 --service。"
        service, route, start, end, found = picked
    elif not found:
        found = E.anomalies(es, ix["anomalies"], start, end, service)

    # 平常：一週前同一段時間；那時候沒資料（例如服務剛上線）就用出事前那一段（同樣長，空 ref_gap 避開剛開始變壞的時候）
    back, length, gap = _dur(cfg.get("compare_with", "7d")), end - start, _dur(cfg.get("ref_gap", "10m"))
    ref_start, ref_end, ref_label = None, None, ""
    for rs, re_, label in ((start - back, end - back, f"{cfg.get('compare_with', '7d')} 前同一段時間"),
                           (start - gap - length, start - gap, "出事前一段時間")):
        if E.entry_stats(es, ix["traces"], service, route, rs, re_)["requests"]:
            ref_start, ref_end, ref_label = rs, re_, label
            break

    what = f"{service}" + (f"（{route}）" if route else "")
    out = [f"# 根因分析：{what}，{_local(start, tz)}～{_local(end, tz)}（{tz}）", ""]

    # ── 1. Metrics ──
    out += ["## 1. Metrics：發現異常", ""]
    if found:
        out += ["| 時間 | 指標 | 哪一段 | 現在 | 平常 | 偏離 |", "|---|---|---|---|---|---|"]
        for r in sorted(found, key=lambda r: -abs(r.get("z") or 0))[:10]:
            z = f"{r['z']:.1f} 倍" if r.get("z") is not None else "規則"
            out.append(f"| {_local(_ts(r['@timestamp']), tz)} | `{r['series']}` | {r['keys'].get('segment', '')} "
                       f"| {r['value']:.4g} | {r['median']:.4g} | {z} |")
        if len(found) > 10:
            out.append(f"| … | 共 {len(found)} 筆 | | | | |")
    else:
        out.append("這段時間 baseline 沒有記到這個服務的異常（直接指定服務查）。")
    out.append("")

    # ── 2. Trace：範圍 ──
    now = E.entry_stats(es, ix["traces"], service, route, start, end)
    out += ["## 2. Trace：這個服務收到的請求", ""]
    if not now["requests"]:
        out.append("這段時間沒有這個服務的 trace（服務沒埋點、取樣太低、或名稱不對）。只能從 log 找。")
        return "\n".join(out)
    ref = E.entry_stats(es, ix["traces"], service, route, ref_start, ref_end) if ref_start else None
    out += [f"| | 現在 | 平常（{ref_label or '沒有資料'}） |", "|---|---|---|"]
    for k, label in (("requests", "請求數（trace，有取樣）"), ("errors", "錯誤"), ("p50_ms", "p50"), ("p95_ms", "p95")):
        f = _ms if k.endswith("_ms") else str
        out.append(f"| {label} | {f(now[k])} | {f(ref[k]) if ref else '—'} |")
    out.append("")

    pad = timedelta(minutes=5)
    now_ids = E.sample_trace_ids(es, ix["traces"], service, route, start, end, n, slowest=True)
    now_tr = T.group_traces(T.spans_of(es, ix["traces"], now_ids, start - pad, end + pad))
    ref_tr = {}
    if ref_start:
        ref_ids = E.sample_trace_ids(es, ix["traces"], service, route, ref_start, ref_end, n, slowest=False)
        ref_tr = T.group_traces(T.spans_of(es, ix["traces"], ref_ids, ref_start - pad, ref_end + pad))

    # ── 3. Trace：瓶頸 ──
    diffs = A.compare_segments(now_tr, ref_tr)
    out += ["## 3. Trace：慢在哪一段", "",
            f"比較最慢、出錯的 {len(now_tr)} 個請求和平常的 {len(ref_tr)} 個請求，每個請求在每一段**自己**花的時間"
            "（扣掉等下游的時間，所以慢的那段不會一路算到最上層）：", "",
            "| 段 | 現在 | 平常 | 多了 | 每個請求呼叫幾次 |", "|---|---|---|---|---|"]
    for d in diffs[:6]:
        out.append(f"| {d.segment} | {_ms(d.now_ms)} | {_ms(d.ref_ms)} | **{_delta(d.delta_ms)}** "
                   f"| {d.now_calls:.1f}（平常 {d.ref_calls:.1f}） |")
    errs = sorted([d for d in diffs if d.now_errors > d.ref_errors], key=lambda d: d.now_errors - d.ref_errors, reverse=True)
    if errs:
        out += ["", "錯誤最早出在哪一段（上層只是把錯誤往上傳）：", "", "| 段 | 現在 | 平常 |", "|---|---|---|"]
        out += [f"| {d.segment} | {d.now_errors} 個請求 | {d.ref_errors} |" for d in errs[:5]]
    out.append("")

    # ── 4. 瀑布圖 ──
    slowest = max(now_tr.values(), key=lambda t: A.root_of(t)["dur_ms"])
    out += [f"## 4. 最慢的那個請求（trace_id `{slowest[0]['trace_id']}`）", "", "```"] + A.waterfall(slowest) + ["```", ""]

    # ── 5. Log ──
    top = [d for d in diffs if d.delta_ms > 0][:3]
    suspects = list(dict.fromkeys([d.segment.split(" › ")[0] for d in top + errs[:3]] + [service]))
    out += ["## 5. Log：那幾段發生了什麼事", "",
            f"看 {', '.join(suspects)} 這段時間的 warn／error，跟{ref_label or '平常'}比；新出現的排前面：", ""]
    p_now = E.problem_logs(es, ix["logs"], suspects, start, end)
    p_ref = E.problem_logs(es, ix["logs"], suspects, ref_start, ref_end) if ref_start else []
    pats = A.compare_patterns(p_now, p_ref)
    if pats:
        out += ["| 服務 | 樣式 | 現在 | 平常 | 例子 |", "|---|---|---|---|---|"]
        for p in pats[:8]:
            ex = p.example.splitlines()[0][:120].replace("|", "\\|")
            out.append(f"| {p.service} | {'🆕 ' if p.is_new else ''}`{p.pattern[:80].replace('|', '/')}` | {p.now} | {p.ref} | {ex} |")
    else:
        out.append("沒有比平常多的 warn／error log。")
    lines = E.logs_of_traces(es, ix["logs"], [slowest[0]["trace_id"]], start - pad, end + pad)
    out += ["", f"最慢那個請求的 log（依 trace_id 從各服務撈出來，{len(lines)} 行）：", "", "```"]
    out += [f"{l['ts']}  {l['service']:<14} {str(l['level'] or ''):<5}  {l['message'].splitlines()[0][:150]}" for l in lines[:20]] or ["（沒有：這幾個服務的 log 沒寫 trace_id？）"]
    out += ["```", ""]

    # ── 6. 呼叫關係 ──
    edges = A.topology(now_tr)
    out += ["## 6. 這些請求經過的服務", "", "```mermaid", A.mermaid(edges), "```", ""]

    # ── 結論 ──
    out += ["## 結論（給人確認）", ""]
    if top:
        d = top[0]
        out.append(f"- {what} 變慢，主要多在 **{d.segment}**：每個請求多花 {_ms(d.delta_ms)}（平常 {_ms(d.ref_ms)}）。")
    if errs:
        out.append(f"- 錯誤最早出在 **{errs[0].segment}**（{errs[0].now_errors} 個請求，平常 {errs[0].ref_errors}）。")
    new = [p for p in pats if p.is_new][:2]
    for p in new:
        out.append(f"- 同時間 {p.service} 新出現 log「{p.example.splitlines()[0][:100]}」，{p.now} 次。")
    if not (top or errs or new):
        out.append("- 跟平常比看不出哪一段變化特別大，可能是整體量變大，或問題不在有 trace 的地方。")
    return "\n".join(out)


def topology(cfg: dict, args) -> str:
    es = E.ES()
    end = _ts(args.at)
    start = end - _dur(args.window)
    ids = E.recent_trace_ids(es, cfg["indices"]["traces"], start, end, args.sample)
    tr = T.group_traces(T.spans_of(es, cfg["indices"]["traces"], ids, start - timedelta(minutes=5), end + timedelta(minutes=5)))
    edges = A.topology(tr)
    tz = cfg.get("timezone", "Asia/Taipei")
    out = [f"# 服務之間的呼叫關係：{_local(start, tz)}～{_local(end, tz)}（{tz}，最近 {len(tr)} 個請求）", "",
           "| 呼叫方 | 被呼叫 | 次數 | 錯誤 | p95 |", "|---|---|---|---|---|"]
    out += [f"| {e.caller} | {e.callee} | {e.calls} | {e.errors} | {_ms(e.p95())} |" for e in edges]
    out += ["", "```mermaid", A.mermaid(edges), "```"]
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aiops_rca")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("investigate")
    i.add_argument("--config", required=True)
    i.add_argument("--service")
    i.add_argument("--route", help="API，trace 裡 Server span 的名稱，例如 \"GET /lots/{id}\"")
    i.add_argument("--start")
    i.add_argument("--end")
    i.add_argument("--at", help="不給 --start/--end 時：查到這個時間為止（預設現在）")
    i.add_argument("--window", default="30m")
    i.add_argument("--out")
    t = sub.add_parser("topology")
    t.add_argument("--config", required=True)
    t.add_argument("--at")
    t.add_argument("--window", default="1h")
    t.add_argument("--sample", type=int, default=2000)
    t.add_argument("--out")
    args = p.parse_args(argv)
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    report = investigate(cfg, args) if args.cmd == "investigate" else topology(cfg, args)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

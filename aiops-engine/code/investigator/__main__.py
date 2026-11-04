"""用法（在 aiops 底下）：

  # 從 baseline 找到的異常開始（最近 30 分鐘裡最嚴重的那個服務）
  python3 -m investigator investigate --config configs/rca/rca.yaml
  # 指定服務、API、時間
  python3 -m investigator investigate --config configs/rca/rca.yaml --service mes-api --route "GET /lots/{id}" \\
      --start 2026-10-10T02:00:00Z --end 2026-10-10T02:15:00Z
  # 服務之間的呼叫關係
  python3 -m investigator topology --config configs/rca/rca.yaml --window 1h
  # 事件整合：最近的異常合成一件、新的那件跑根因分析，寫 aiops-alerts（Grafana 讀它發通知）。每 1～2 分鐘跑一次
  python3 -m investigator alert --config configs/rca/rca.yaml [--at …]
  # 相似事故檢索的評測（留一法 recall@3）
  python3 -m investigator similar-eval --config configs/rca/rca.yaml
  # 歷史事故單匯入（CSV 或 JSON，欄位＝aiops-incidents；--dry-run 只檢查不寫）
  python3 -m investigator import-incidents configs/rca/incidents-example.csv [--tz Asia/Taipei] [--dry-run]

investigate 照「Metrics 發現異常 → Trace 縮小到哪一段 → Log 找出原因」的順序查，印出一份 Markdown 報告；
--out 另存成檔案。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone

import yaml

from core import traces as T

from . import analyze as A
from . import cmd_alert
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
    return keys.get("resource.attributes.service.name") or keys.get("entry.service") or keys.get("service")


def _route_of(keys: dict) -> str | None:
    """哪支 API（trace 裡 Server span 的名稱，例如 "GET /lots/{id}"）。"""
    if keys.get("entry.name"):
        return keys["entry.name"]
    if keys.get("attributes.http.request.method") and keys.get("attributes.http.route"):
        return f"{keys['attributes.http.request.method']} {keys['attributes.http.route']}"
    if keys.get("method") and keys.get("route"):          # http.* 序列統一後的 keys
        return f"{keys['method']} {keys['route']}"
    if keys.get("route"):                                 # APM 的 transaction.name 本來就是 "GET /api/x"，沒有分開的 method
        return keys["route"]
    return None


def _trust(r: dict) -> str:
    """異常用的 baseline 可不可信：可信度、粒度、方法、借誰的。規則、跟兄弟比沒有這些欄位。"""
    if not r.get("confidence"):
        return ""
    src = r.get("baseline_source") or "self"
    extra = [r.get("resolution") or "", "卜瓦松" if r.get("method") == "poisson" else "",
             f"借 {src[len('group:'):]}" if src.startswith("group:") else ""]
    return r["confidence"] + "".join(f"、{x}" for x in extra if x)


def pick_from_anomalies(es, cfg, start, end):
    """沒指定服務：從 baseline 寫的異常裡，挑這段時間最嚴重的服務（偏離倍數最大），時間範圍取它異常的那段。
    異常帶著 method、API 的話（http.* 序列），也只查那支 API。"""
    rows = E.anomalies(es, cfg["indices"]["anomalies"], start, end)
    # warning（只跳一次、或即時檢查的）不觸發根因分析；沒有 status 的是規則、跟兄弟比，照算
    rows = [r for r in rows if _svc_of(r.get("keys") or {}) and r.get("status", "anomaly") == "anomaly"]
    if not rows:
        return None
    top = max(rows, key=lambda r: abs(r.get("z") or 0))
    svc, route = _svc_of(top["keys"]), _route_of(top["keys"])
    mine = [r for r in rows if _svc_of(r["keys"]) == svc]
    t = sorted(_ts(r["@timestamp"]) for r in mine)
    # 異常的時間是那 5 分鐘的開始，所以查到最後一個異常再往後 5 分鐘
    return svc, route, t[0], t[-1] + _dur(cfg.get("interval", "5m")), mine


def investigate(cfg: dict, args) -> str:
    return investigate_full(cfg, args)[0]


def investigate_full(cfg: dict, args) -> tuple[str, dict]:
    """回傳 (Markdown 報告, findings)。findings 是結構化的結果：異常、慢在哪一段、錯從哪開始、新 log 樣式、呼叫關係，
    給事件整合存進 aiops-alerts、相似事故檢索（investigator/similar.py）和 LLM 摘要（core/llm.py）用。"""
    es = E.ES()
    findings: dict = {"service": None, "route": None, "anomalies": [], "segments": [], "error_origins": [], "new_patterns": [],
                      "topology": [], "requests": None, "ref_label": None, "trend": None, "commonality": []}
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
            return f"{_local(start, tz)}～{_local(end, tz)} 沒有 baseline 找到的異常。要查特定服務的話用 --service。", findings
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
    findings.update({"service": service, "route": route, "start": start.isoformat(), "end": end.isoformat(), "ref_label": ref_label or None,
                     "anomalies": [{"id": r.get("_id"), "series": r.get("series"), "value": r.get("value"), "median": r.get("median"),
                                    "z": r.get("z"), "status": r.get("status"), "severity": r.get("severity"), "at": r.get("@timestamp")}
                                   for r in sorted(found, key=lambda r: -abs(r.get("z") or 0))[:10]]})
    out = [f"# 根因分析：{what}，{_local(start, tz)}～{_local(end, tz)}（{tz}）", ""]

    # ── 1. Metrics ──
    out += ["## 1. Metrics：發現異常", ""]
    if found:
        out += ["| 時間 | 指標 | 哪一段 | 現在 | 平常 | 偏離 | 可信度 |", "|---|---|---|---|---|---|---|"]
        for r in sorted(found, key=lambda r: -abs(r.get("z") or 0))[:10]:
            z = (f"機率 {r['p']:.1e}" if r.get("method") == "poisson" and r.get("p") is not None
                 else f"{r['z']:.1f} 倍" if r.get("z") is not None else "規則")
            med = f"{r['median']:.4g}" if r.get("median") is not None else "—"
            warn = "（warning）" if r.get("status") == "warning" else ""
            out.append(f"| {_local(_ts(r['@timestamp']), tz)}{warn} | `{r['series']}` | {r['keys'].get('segment', '')} "
                       f"| {r['value']:.4g} | {med} | {z} | {_trust(r)} |")
        if len(found) > 10:
            out.append(f"| … | 共 {len(found)} 筆 | | | | | |")
        if any(r.get("confidence") == "low" for r in found):
            out += ["", "> 可信度 low：用的是不分時段、或借同類 API 的 baseline（不是自己的歷史），先確認是不是真的變了。"]
    else:
        out.append("這段時間 baseline 沒有記到這個服務的異常（直接指定服務查）。")
    out.append("")

    # ── 2. Trace：範圍 ──
    now = E.entry_stats(es, ix["traces"], service, route, start, end)
    out += ["## 2. Trace：這個服務收到的請求", ""]
    findings["requests"] = now
    if not now["requests"]:
        out.append("這段時間沒有這個服務的 trace（服務沒埋點、或名稱不對；剛開件時 trace 還沒合成完也會這樣，合成晚 1～2 分鐘）。只能先從 log 找；沒有 trace 的件系統會自動重跑報告（每 2 分鐘、最多 5 次）。")
        return "\n".join(out), findings
    ref = E.entry_stats(es, ix["traces"], service, route, ref_start, ref_end) if ref_start else None
    out += [f"| | 現在 | 平常（{ref_label or '沒有資料'}） |", "|---|---|---|"]
    for k, label in (("requests", "請求數（trace，有取樣）"), ("errors", "錯誤"), ("p50_ms", "p50"), ("p95_ms", "p95")):
        f = _ms if k.endswith("_ms") else str
        out.append(f"| {label} | {f(now[k])} | {f(ref[k]) if ref else '—'} |")
    out.append("")
    # 出事前的趨勢：前面幾段（預設 6 段、每段 10 分鐘）的 p95，一路往上＝慢慢惡化、平的然後跳＝突然
    tcfg = cfg.get("trend") or {}
    tw, tn = _dur(str(tcfg.get("window", "10m"))), int(tcfg.get("windows", 6))
    p95s = [E.entry_stats(es, ix["traces"], service, route, start - tw * (k + 1), start - tw * k)["p95_ms"] for k in reversed(range(tn))]
    trend = A.classify_trend(p95s, now["p95_ms"])
    trend["window"], trend["windows"] = str(tcfg.get("window", "10m")), tn
    findings["trend"] = trend
    span_label = f"{int(tw.total_seconds() * tn // 60)} 分鐘"
    out += [A.describe_trend(trend, span_label, _ms), ""]

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
    findings["segments"] = [{"segment": d.segment, "now_ms": d.now_ms, "ref_ms": d.ref_ms, "delta_ms": d.delta_ms,
                             "now_calls": d.now_calls, "ref_calls": d.ref_calls} for d in diffs[:6]]
    findings["error_origins"] = [{"segment": d.segment, "now_errors": d.now_errors, "ref_errors": d.ref_errors} for d in errs[:5]]
    if errs:
        out += ["", "錯誤最早出在哪一段（上層只是把錯誤往上傳）：", "", "| 段 | 現在 | 平常 |", "|---|---|---|"]
        out += [f"| {d.segment} | {d.now_errors} 個請求 | {d.ref_errors} |" for d in errs[:5]]
    out.append("")

    # ── 3b. 慢請求的共同點 ──
    ccfg = cfg.get("commonality") or {}
    commons = A.commonality(now_tr, ref_tr, res_keys=tuple(ccfg.get("resource") or ("k8s.pod.name", "service.version", "k8s.node.name")),
                            attr_keys=tuple(ccfg.get("attrs") or ("lot.id", "db.statement")),
                            min_share=float(ccfg.get("min_share", 0.6)), min_lift=float(ccfg.get("min_lift", 2)))
    findings["commonality"] = [{"dim": c.dim, "value": c.value, "now": c.now, "now_share": c.now_share, "ref_share": c.ref_share}
                               for c in commons[:5]]
    if commons:
        out += ["這批慢的請求有什麼共同點（平常沒這麼集中）：", "", "| 看什麼 | 值 | 慢的請求裡 | 平常 |", "|---|---|---|---|"]
        out += [f"| {c.dim} | `{c.value}` | {c.now_share:.0%}（{c.now} 個） | {'平常沒見過（換版、新 Pod？）' if c.is_new else f'{c.ref_share:.0%}'} |"
                for c in commons[:5]]
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
    findings["new_patterns"] = [{"service": p.service, "pattern": p.pattern[:200], "now": p.now, "ref": p.ref, "is_new": bool(p.is_new),
                                 "example": p.example.splitlines()[0][:200]} for p in pats[:8]]
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
    findings["topology"] = [{"caller": e.caller, "callee": e.callee, "calls": e.calls, "errors": e.errors} for e in edges]
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
    if commons:
        c = commons[0]
        if c.is_new:
            out.append(f"- 慢的請求 {c.now_share:.0%} 都在平常沒見過的 {c.dim} `{c.value}`：像是換版後的新 Pod／新版本，先確認那時有沒有部署。")
        else:
            out.append(f"- 慢的請求 {c.now_share:.0%} 都是 {c.dim} = `{c.value}`（平常 {c.ref_share:.0%}）：問題可能只在它身上。")
    if trend.get("kind") in ("gradual", "sudden"):
        out.append("- " + A.describe_trend(trend, span_label, _ms).split("→ ", 1)[1].replace("**", ""))
    if not (top or errs or new):
        out.append("- 跟平常比看不出哪一段變化特別大，可能是整體量變大，或問題不在有 trace 的地方。")
    return "\n".join(out), findings


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
    p = argparse.ArgumentParser(prog="investigator")
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
    al = sub.add_parser("alert", help="異常合成一件、新的那件跑根因分析，寫 aiops-alerts（Grafana 讀它發通知）")
    al.add_argument("--config", required=True)
    al.add_argument("--at", help="回測：拿這個時間當成「現在」")
    se = sub.add_parser("similar-eval", help="告警分類的評測：留一法分類準確率與 recall@3（同一個根因分類有沒有排進前 3）")
    se.add_argument("--config", required=True)
    se.add_argument("--learn-weights", action="store_true", help="case ≥ 50 件時用留一法準確率學特徵權重，印出來（不自動寫入）")
    im = sub.add_parser("import-incidents", help="歷史事故單匯入 aiops-incidents（status confirmed、source import），告警分類馬上用得到")
    im.add_argument("file", nargs="?", help="CSV 或 JSON 陣列；欄位見 configs/rca/README.md「匯入歷史事故單」，範例 configs/rca/incidents-example.csv；用 --db 時不給")
    im.add_argument("--db", help="從資料庫讀：SQLAlchemy 的 URL（sqlite:///x.db、postgresql://…、mssql+pyodbc://…）；密碼可寫 ${環境變數}")
    im.add_argument("--sql", help="搭 --db：SELECT 一句，欄位別名要跟 CSV 的欄位名一樣（title、start、end、services、root_cause_category…）")
    im.add_argument("--config", help="選填：拿 timezone 當沒寫時區的時間的時區；incidents_db: {url, sql} 可以放這裡代替 --db、--sql")
    im.add_argument("--tz", help="沒寫時區的時間當哪裡的時間（預設 Asia/Taipei，或 --config 的 timezone）")
    im.add_argument("--dry-run", action="store_true", help="只解析、印出哪幾筆會略過，不寫 ES")
    args = p.parse_args(argv)
    cfg = {}
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    if args.cmd == "import-incidents":
        from . import importer as IMP
        db = cfg.get("incidents_db") or {}
        url, sql = args.db or db.get("url"), args.sql or db.get("sql")
        if url:
            if not sql:
                print("--db 要搭 --sql（或 config 的 incidents_db.sql）")
                return 2
            return IMP.run(IMP.DbRows(url, sql), tz=args.tz or cfg.get("timezone") or IMP.DEFAULT_TZ, dry_run=args.dry_run)
        if not args.file:
            print("要給檔案，或 --db + --sql")
            return 2
        return IMP.run(args.file, tz=args.tz or cfg.get("timezone") or IMP.DEFAULT_TZ, dry_run=args.dry_run)
    if args.cmd == "alert":
        return cmd_alert.run(cfg, args, investigate_full)
    if args.cmd == "similar-eval":
        from . import similar as SIM
        es = E.ES()
        weights = (cfg.get("alerts") or {}).get("similar_weights")
        r = SIM.evaluate(es, weights=weights)
        for row in r["rows"]:
            pred = f"→ 分類 {row['predicted']}（{row['confidence']:.0%}）" if row["predicted"] else "→ 沒有相似的、不分類"
            print(f"  {'✓' if row['correct'] else '✗'} {row['id'][:24]:<24} [{row['category']}] {row['title'] or ''}  {pred}")
        if r["n"]:
            print(f"分類準確率：{r['correct']}/{r['n']}（{r['accuracy']:.0%}）；recall@3：{r['hit']}/{r['n']}（{r['recall']:.0%}）"
                  + (f"；權重 {weights}" if weights else ""))
        else:
            print("沒有已確認、有根因分類的事故")
        if args.learn_weights:
            got = SIM.learn_weights(SIM._confirmed_cases(es), start=weights)
            if not got["ok"]:
                print(got["reason"])
            else:
                print(f"學出來的權重：{got['weights']}（準確率 {got['before']:.0%} → {got['after']:.0%}，{got['n']} 件）；"
                      f"要用的話寫進 rca.yaml 的 alerts.similar_weights")
        return 0
    report = investigate(cfg, args) if args.cmd == "investigate" else topology(cfg, args)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

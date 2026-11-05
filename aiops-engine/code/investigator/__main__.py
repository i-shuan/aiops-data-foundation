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

investigate 照「Metrics 發現異常 → Trace 縮小到哪一段 → Log 找出原因」的順序查，印出一份 Markdown 報告；報告有哪幾段由 plugin 決定
（investigator/report.py、core/plugin.py）。--out 另存成檔案。
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta

import yaml

from core import traces as T

from . import analyze as A
from . import cmd_alert
from . import es as E
from .report import _dur, _local, _ms, _ts, investigate_full  # noqa: F401  舊的 import 路徑還有人用


def investigate(cfg: dict, args) -> str:
    return investigate_full(cfg, args)[0]


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

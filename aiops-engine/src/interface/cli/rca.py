"""用法（在 aiops 底下）：

  # 從 baseline 找到的異常開始（最近 30 分鐘裡最嚴重的那個服務）
  python3 -m main investigator investigate --config configs/rca/rca.yaml
  # 指定服務、API、時間
  python3 -m main investigator investigate --config configs/rca/rca.yaml --service mes-api --route "GET /lots/{id}" \\
      --start 2026-10-10T02:00:00Z --end 2026-10-10T02:15:00Z
  # 服務之間的呼叫關係
  python3 -m main investigator topology --config configs/rca/rca.yaml --window 1h
  # 事件整合：最近的異常合成一件、新的那件跑根因分析，寫 aiops-alerts（Grafana 讀它發通知）。每 1～2 分鐘跑一次
  python3 -m main investigator alert --config configs/rca/rca.yaml [--at …]
  # 相似事故檢索的評測（留一法 recall@3）
  python3 -m main investigator similar-eval --config configs/rca/rca.yaml
  # 歷史事故單匯入（CSV 或 JSON，欄位＝aiops-incidents；--dry-run 只檢查不寫）
  python3 -m main investigator import-incidents configs/rca/incidents-example.csv [--tz Asia/Taipei] [--dry-run]

investigate 照「Metrics 發現異常 → Trace 縮小到哪一段 → Log 找出原因」的順序查，印出一份 Markdown 報告；報告有哪幾段由 plugin 決定
（incident/rca_report.py、Scenario.sections）。--out 另存成檔案。
命令列用 typer（跟 detector 一樣）：@app.command 的函式把參數裝成 argparse.Namespace、用 core.config.load_config 讀 rca.yaml，再交給處理函式。
"""
from __future__ import annotations

import sys
from argparse import Namespace
from datetime import timedelta

import typer

import detect.store.trace as T
from shared.schema.loader import load_config
from shared.connection import ES
from shared.repos import Repos
from shared.scenario import load_scenario

import incident.rca_analyzer as A
import incident.alert_service as cmd_alert
import llm.similar as SIM
import learn.history_importer as IMP
from incident.rca_report import _dur, _local, _ms, _ts, investigate_full  # noqa: F401  舊的 import 路徑還有人用

app = typer.Typer(add_completion=False, rich_markup_mode=None, pretty_exceptions_enable=False)
Opt = typer.Option


def investigate(cfg: dict, args) -> str:
    return investigate_full(cfg, args)[0]


def topology(cfg: dict, args) -> str:
    traces, ix = Repos(ES(), load_scenario()).traces, cfg["indices"]["traces"]
    end = _ts(args.at)
    start = end - _dur(args.window)
    ids = traces.recent_trace_ids(ix, start, end, args.sample)
    tr = T.group_traces(traces.spans(ix, ids, start - timedelta(minutes=5), end + timedelta(minutes=5)))
    edges = T.topology(tr)
    tz = cfg.get("timezone", "Asia/Taipei")
    out = [f"# 服務之間的呼叫關係：{_local(start, tz)}～{_local(end, tz)}（{tz}，最近 {len(tr)} 個請求）", "",
           "| 呼叫方 | 被呼叫 | 次數 | 錯誤 | p95 |", "|---|---|---|---|---|"]
    out += [f"| {e.caller} | {e.callee} | {e.calls} | {e.errors} | {_ms(e.p95())} |" for e in edges]
    out += ["", "```mermaid", T.mermaid(edges), "```"]
    return "\n".join(out)


def similar_eval(cfg: dict, args) -> int:
    repo = Repos(ES(), load_scenario())
    weights = (cfg.get("alerts") or {}).get("similar_weights")
    r = SIM.evaluate(repo, weights=weights)
    for row in r["rows"]:
        pred = f"→ 分類 {row['predicted']}（{row['confidence']:.0%}）" if row["predicted"] else "→ 沒有相似的、不分類"
        print(f"  {'✓' if row['correct'] else '✗'} {row['id'][:24]:<24} [{row['category']}] {row['title'] or ''}  {pred}")
    if r["n"]:
        print(f"分類準確率：{r['correct']}/{r['n']}（{r['accuracy']:.0%}）；recall@3：{r['hit']}/{r['n']}（{r['recall']:.0%}）"
              + (f"；權重 {weights}" if weights else ""))
    else:
        print("沒有已確認、有根因分類的事故")
    if args.learn_weights:
        got = SIM.learn_weights(SIM._confirmed_cases(repo), start=weights)
        if not got["ok"]:
            print(got["reason"])
        else:
            print(f"學出來的權重：{got['weights']}（準確率 {got['before']:.0%} → {got['after']:.0%}，{got['n']} 件）；"
                  f"要用的話寫進 rca.yaml 的 alerts.similar_weights")
    return 0


def import_incidents(cfg: dict, args) -> int:
    db = cfg.get("incidents_db") or {}
    url, sql = args.db or db.get("url"), args.sql or db.get("sql")
    tz = args.tz or cfg.get("timezone") or IMP.DEFAULT_TZ
    if url:
        if not sql:
            print("--db 要搭 --sql（或 config 的 incidents_db.sql）")
            return 2
        return IMP.run(IMP.DbRows(url, sql), tz=tz, dry_run=args.dry_run)
    if not args.file:
        print("要給檔案，或 --db + --sql")
        return 2
    return IMP.run(args.file, tz=tz, dry_run=args.dry_run)


def _report(text: str, out: str | None) -> int:
    print(text)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


@app.command("investigate")
def investigate_cmd(config: str = Opt(...), service: str | None = Opt(None),
                    route: str | None = Opt(None, help="API，trace 裡 Server span 的名稱，例如 \"GET /lots/{id}\""),
                    start: str | None = Opt(None), end: str | None = Opt(None),
                    at: str | None = Opt(None, help="不給 --start/--end 時：查到這個時間為止（預設現在）"),
                    window: str = Opt("30m"), out: str | None = Opt(None)) -> int:
    args = Namespace(config=config, service=service, route=route, start=start, end=end, at=at, window=window, out=out)
    return _report(investigate(load_config(config, "rca"), args), out)


@app.command("topology")
def topology_cmd(config: str = Opt(...), at: str | None = Opt(None), window: str = Opt("1h"), sample: int = Opt(2000),
                 out: str | None = Opt(None)) -> int:
    args = Namespace(config=config, at=at, window=window, sample=sample, out=out)
    return _report(topology(load_config(config, "rca"), args), out)


@app.command("alert", help="異常合成一件、新的那件跑根因分析，寫 aiops-alerts（Grafana 讀它發通知）")
def alert_cmd(config: str = Opt(...), at: str | None = Opt(None, help="回測：拿這個時間當成「現在」")) -> int:
    return cmd_alert.run(load_config(config, "rca"), Namespace(config=config, at=at), investigate_full)


@app.command("similar-eval", help="告警分類的評測：留一法分類準確率與 recall@3（同一個根因分類有沒有排進前 3）")
def similar_eval_cmd(config: str = Opt(...),
                     learn_weights: bool = Opt(False, "--learn-weights", help="case ≥ 50 件時用留一法準確率學特徵權重，印出來（不自動寫入）")) -> int:
    return similar_eval(load_config(config, "rca"), Namespace(config=config, learn_weights=learn_weights))


@app.command("import-incidents", help="歷史事故單匯入 aiops-incidents（status confirmed、source import），告警分類馬上用得到")
def import_incidents_cmd(file: str | None = typer.Argument(None, help="CSV 或 JSON 陣列；欄位見 configs/rca/README.md「匯入歷史事故單」，"
                                                                      "範例 configs/rca/incidents-example.csv；用 --db 時不給"),
                         db: str | None = Opt(None, help="從資料庫讀：SQLAlchemy 的 URL（sqlite:///x.db、postgresql://…、mssql+pyodbc://…）；密碼可寫 ${環境變數}"),
                         sql: str | None = Opt(None, help="搭 --db：SELECT 一句，欄位別名要跟 CSV 的欄位名一樣（title、start、end、services、root_cause_category…）"),
                         config: str | None = Opt(None, help="選填：拿 timezone 當沒寫時區的時間的時區；incidents_db: {url, sql} 可以放這裡代替 --db、--sql"),
                         tz: str | None = Opt(None, help="沒寫時區的時間當哪裡的時間（預設 Asia/Taipei，或 --config 的 timezone）"),
                         dry_run: bool = Opt(False, "--dry-run", help="只解析、印出哪幾筆會略過，不寫 ES")) -> int:
    args = Namespace(file=file, db=db, sql=sql, config=config, tz=tz, dry_run=dry_run)
    return import_incidents(load_config(config, "rca") if config else {}, args)


def main(argv=None) -> int:
    try:
        rv = app(args=argv, prog_name="investigator", standalone_mode=False)
    except typer.TyperException as e:          # 參數錯：印用法、回 2，跟 argparse 一樣
        if not hasattr(e, "exit_code"):
            raise
        e.show()
        return e.exit_code
    return int(rv or 0)


if __name__ == "__main__":
    sys.exit(main())

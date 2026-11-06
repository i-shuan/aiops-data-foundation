"""⑨ 的命令列（掛在 python3 -m main detector 底下，名字跟以前一樣）：
  feedback pending|add|list|summary|suggest|export    值班回饋
  incident add|list|resolve                           事故紀錄
  adjust add|list|approve|remove                      排除時段、重置點、週期放寬
  backtest、metrics
每個子命令是一個函式、參數就是它自己的命令列參數（不再是一個 action 參數配 20 幾個選項）；處理函式在 feedback/cmd_*.py。
detector/__main__.py 把 COMMANDS 跟自己的合起來建 app。"""
from __future__ import annotations

from typing import Callable, List, Literal

import typer

from interface.cli.common import SERIES, Opt, group, registry, run

from service.feedback.adjust_service import adjust_add, adjust_approve, adjust_list, adjust_remove
from service.feedback.backtest import cmd_backtest
from service.feedback.feedback_service import (feedback_add, feedback_export, feedback_list, feedback_pending, feedback_suggest, feedback_summary,
                           incident_add, incident_list, incident_resolve)
from service.feedback.metrics import cmd_metrics

COMMANDS: dict[str, Callable[[typer.Typer], None]] = {}
command = registry(COMMANDS)

# ── feedback ──
feedback = group("feedback", help="值班回饋：pending（還沒標記的）、add、list、summary（誤報統計）、suggest（誤報多次→建議放寬）、"
                                  "export（回饋＋異常匯出成 NDJSON／CSV，給 ML 團隊重訓；工廠版）")
COMMANDS["feedback"] = lambda app: app.add_typer(feedback)


@feedback.command("pending", help="還沒人標記的異常")
def feedback_pending_cmd(config: str = Opt(SERIES), days: int = Opt(28), limit: int = Opt(50),
                         series: str | None = Opt(None, help="序列名稱包含")) -> int:
    return run(feedback_pending, "series", config=config, days=days, limit=limit, series=series)


@feedback.command("list")
def feedback_list_cmd(config: str = Opt(SERIES), days: int = Opt(28), limit: int = Opt(50)) -> int:
    return run(feedback_list, "series", config=config, days=days, limit=limit)


@feedback.command("summary", help="誤報統計")
def feedback_summary_cmd(config: str = Opt(SERIES), days: int = Opt(28),
                         min_false_positive: int = Opt(1, help="同一個對象誤報幾次才列入（預設 1）")) -> int:
    return run(feedback_summary, "series", config=config, days=days, min_false_positive=min_false_positive)


@feedback.command("suggest", help="誤報多次→放寬、漏報→收緊、同根因 ≥ N 次→升級成規則；全部 proposed，adjust approve 才生效")
def feedback_suggest_cmd(config: str = Opt(SERIES), days: int = Opt(28),
                         min_false_positive: int = Opt(1, help="同一個對象誤報幾次才提建議（預設 1：有人標就提，不累積）"),
                         min_recurring: int = Opt(3, help="同一個服務、同一個根因分類確認幾次才提「升級成規則」（預設 3）")) -> int:
    return run(feedback_suggest, "series", config=config, days=days, min_false_positive=min_false_positive, min_recurring=min_recurring)


@feedback.command("export", help="回饋＋異常匯出成 NDJSON／CSV（給 ML 團隊重訓）")
def feedback_export_cmd(config: str = Opt(SERIES), days: int = Opt(28),
                        out: str | None = Opt(None, help="寫到哪個檔（.ndjson 或 .csv；不給就印到 stdout）")) -> int:
    return run(feedback_export, "series", config=config, days=days, out=out)


@feedback.command("add", help="一筆判斷：--anomaly <id> 配 --verdict，或 --text 一句話讓系統抽；漏報給 --verdict missed --series --match --at")
def feedback_add_cmd(config: str = Opt(SERIES),
                     anomaly: str | None = Opt(None, help="異常的 id（pending 看得到）"),
                     verdict: str | None = Opt(None, help="true_positive（真的）、false_positive（誤報）、wrong_root_cause（根因錯）、missed（漏報）"),
                     note: str | None = Opt(None), by: str | None = Opt(None),
                     incident: str | None = Opt(None, help="屬於哪件事故"),
                     alert: str | None = Opt(None, help="屬於哪件告警（aiops-alerts 的 alert_id；--text 抽取時的服務、序列從它帶入）"),
                     text: str | None = Opt(None, help="一句自然語言（例：這是每月初的批次匯出報表，屬於預期內負載）；抽成四欄、對得到的提一筆調整建議；"
                                                       "沒給 --verdict 就用抽出來的"),
                     root_cause: str | None = Opt(None, help="正確的根因（wrong_root_cause 時）"),
                     series: str | None = Opt(None, help="漏報：哪條序列"),
                     match: List[str] | None = Opt(None, help="漏報：對象，例如 --match service=mes-api"),
                     at: str | None = Opt(None, help="漏報：漏報的時間")) -> int:
    return run(feedback_add, "series", config=config, anomaly=anomaly, verdict=verdict, note=note, by=by, incident=incident, alert=alert,
               text=text, root_cause=root_cause, series=series, match=match, at=at)


# ── incident ──
incident = group("incident", help="事故紀錄：add、list、resolve")
COMMANDS["incident"] = lambda app: app.add_typer(incident)


@incident.command("list")
def incident_list_cmd(config: str = Opt(SERIES)) -> int:
    return run(incident_list, "series", config=config)


@incident.command("resolve")
def incident_resolve_cmd(config: str = Opt(SERIES), id_: str = Opt(..., "--id"),
                         root_cause: str | None = Opt(None), action_taken: str | None = Opt(None, help="處置")) -> int:
    return run(incident_resolve, "series", config=config, id_=id_, root_cause=root_cause, action_taken=action_taken)


@incident.command("add")
def incident_add_cmd(config: str = Opt(SERIES), id_: str | None = Opt(None, "--id"),
                     title: str | None = Opt(None), start: str | None = Opt(None), end: str | None = Opt(None),
                     service: List[str] | None = Opt(None, help="影響的服務（--service a --service b）"),
                     severity: str | None = Opt(None, help="例如 sev1、sev2"),
                     root_cause: str | None = Opt(None), action_taken: str | None = Opt(None, help="處置"),
                     anomaly: List[str] | None = Opt(None, help="相關的異常 id"), by: str | None = Opt(None),
                     exclude_training: bool = Opt(False, "--exclude-training", help="同時提一筆「這段時間不學」的建議（proposed，adjust approve 才生效）")) -> int:
    return run(incident_add, "series", config=config, id_=id_, title=title, start=start, end=end, service=service, severity=severity,
               root_cause=root_cause, action_taken=action_taken, anomaly=anomaly, by=by, exclude_training=exclude_training)


# ── adjust ──
adjust = group("adjust", help="排除時段、重置點、週期放寬（寫進 aiops-tuning）：add、list、approve、remove")
COMMANDS["adjust"] = lambda app: app.add_typer(adjust)


@adjust.command("list")
def adjust_list_cmd(config: str = Opt(SERIES)) -> int:
    return run(adjust_list, "series", config=config)


@adjust.command("remove")
def adjust_remove_cmd(config: str = Opt(SERIES), id_: str = Opt(..., "--id", help="adjust list 看得到")) -> int:
    return run(adjust_remove, "series", config=config, id_=id_)


@adjust.command("approve", help="proposed → active；override 類要先 backtest，--force 跳過")
def adjust_approve_cmd(config: str = Opt(SERIES), id_: str = Opt(..., "--id", help="adjust list 看得到"), by: str | None = Opt(None),
                       force: bool = Opt(False, "--force", help="沒回測、或回測不建議也硬核准")) -> int:
    return run(adjust_approve, "series", config=config, id_=id_, by=by, force=force)


@adjust.command("add")
def adjust_add_cmd(config: str = Opt(SERIES),
                   kind: Literal["exclude_window", "reset_point", "recurring_override"] = Opt("exclude_window"),
                   id_: str | None = Opt(None, "--id", help="指定 id（同 id 再寫會覆蓋）"),
                   series: str | None = Opt(None, help="哪些序列（正規表示式，不給就是全部）"),
                   match: List[str] | None = Opt(None, help="對象，例如 --match service=^mes-api$（正規表示式；欄位可用別名：service、namespace、pod）"),
                   start: str | None = Opt(None, help="exclude_window：開始（ISO 時間，例如 2026-10-20T02:00:00+08:00）"),
                   end: str | None = Opt(None, help="exclude_window：結束"),
                   apply_to: str = Opt("train", help="exclude_window：train（不學，預設）、train,detect（也不報）"),
                   at: str | None = Opt(None, help="reset_point：從這個時間點起重新學"),
                   warmup: str | None = Opt(None, help="reset_point：重置後多久內不判斷（預設 24h）"),
                   cron: str | None = Opt(None, help="recurring_override：開始時間（5 欄 cron，台北時間），例如 '0 20 * * *'"),
                   duration: str | None = Opt(None, help="recurring_override：持續多久，例如 4h"),
                   days_of_month: str | None = Opt(None, help="recurring_override：哪幾天，-1 是每月最後一天（逗號分開）"),
                   z: float | None = Opt(None, "--z", help="recurring_override：這段時間 z 至少要多少才算"),
                   min_abs: float | None = Opt(None, help="recurring_override：這段時間至少差多少才算"),
                   mute: bool = Opt(False, "--mute", help="recurring_override：這段時間不報"),
                   train: Literal["keep", "exclude"] | None = Opt(None, help="recurring_override：這段時間要不要學（預設 keep）"),
                   status: Literal["active", "proposed"] = Opt("active"),
                   source: str = Opt("manual", help="manual、feedback、cicd"),
                   expires: str | None = Opt(None, help="到期時間（選填）"),
                   reason: str | None = Opt(None), by: str | None = Opt(None)) -> int:
    return run(adjust_add, "series", config=config, kind=kind, id_=id_, series=series, match=match, start=start, end=end, apply_to=apply_to,
               at=at, warmup=warmup, cron=cron, duration=duration, days_of_month=days_of_month, z=z, min_abs=min_abs, mute=mute, train=train,
               status=status, source=source, expires=expires, reason=reason, by=by)


# ── backtest、metrics ──
@command("backtest", help="改門檻前先回測：一筆或一組建議用過去 N 週重跑，看告警少幾個、已確認事故還抓不抓得到；一組有效才逐筆剔除看哪筆多餘")
def backtest(config: str = Opt(SERIES),
             id_: List[str] = Opt(..., "--id", help="aiops-tuning 的建議 id（adjust list 看得到）；給好幾個（--id a --id b）就一組一起測"),
             group_: bool = Opt(False, "--group", help="把同一個對象（同序列、同 match）其他還是 proposed 的建議一起拉進來當一組"),
             weeks: int = Opt(4), at: str | None = Opt(None), verbose: bool = Opt(False, "--verbose")) -> int:
    return run(cmd_backtest, "series", config=config, id_=id_, group_=group_, weeks=weeks, at=at, verbose=verbose)


@command("metrics", help="量化：每天幾件、有回饋的比例、誤報率、從告警到回饋多久；兩段時間並排比「學之前 vs 學之後」")
def metrics(config: str = Opt(SERIES),
            start: str = Opt(..., "--from", help="例如 2026-10-01"), end: str = Opt(..., "--to"),
            cstart: str | None = Opt(None, "--compare-from", help="另一段（學之前）"), cend: str | None = Opt(None, "--compare-to"),
            out: str | None = Opt(None)) -> int:
    return run(cmd_metrics, "series", config=config, start=start, end=end, cstart=cstart, cend=cend, out=out)

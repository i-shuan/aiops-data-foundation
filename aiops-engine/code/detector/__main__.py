"""用法（在 engine 底下；每個指令的程式在 detector/cmd_*.py，這裡只有參數與分派）：

  python3 -m detector train  --config configs/baseline/series.yaml [--weeks 4] [--end 2026-10-10T00:00:00Z]
  python3 -m detector detect --config configs/baseline/series.yaml [--window 30m] [--at 2026-10-10T08:00:00Z]
  python3 -m detector tune   --series http.latency_p95_s --match service=mes-api --resolution 5m --reason 誤報多
  python3 -m detector adjust add --kind exclude_window --match service=mes-api --start … --end … --reason 事故
  python3 -m detector adjust list | approve --id … | remove --id …
  python3 -m detector rules  --config configs/baseline/rules.yaml  [--at …] [--only 規則,…]
  python3 -m detector peer   --config configs/baseline/peer.yaml   [--at …] [--only 檢查,…]
  python3 -m detector quality --config configs/baseline/quality.yaml [--at …] [--only 檢查,…]
  python3 -m detector chronic --config configs/baseline/chronic.yaml [--at …]
  python3 -m detector feedback pending|add|list|summary|suggest   值班回饋（真的／誤報／根因錯／漏報）
  python3 -m detector incident add|list|resolve                   事故紀錄

train ：讀過去 N 週的資料，每條序列、每個時段學出中位數與波動範圍，寫進 ES 的 aiops-baseline。
detect：讀最近一段時間的資料，跟 baseline 比，超出範圍的寫進 aiops-anomalies，並印出來。
        跳一次是 warning，同一條序列連續兩個時間窗超標（或連續超標滿 30 分鐘）才是 anomaly。
        預設 30 秒一點（跟收值一樣），每 30 秒跑一次：連續兩個 30 秒（約 1 分鐘）就是 anomaly。
tune  ：調整某條序列、某些對象的粒度、z、min_abs（寫進 aiops-tuning；之後 feedback 也寫這裡），下次 train／detect 生效。
        --show 看某條序列、某個對象現在生效的值是多少、來自哪一筆（多筆都符合時後面蓋前面）。
adjust：排除時段（事故、維護、壓測不學）、重置點（換版後只學新的）、週期放寬（月底批次放寬或不報），
        也寫 aiops-tuning（detector/adjust.py）。status: proposed 的要 approve 才生效。
rules ：狀態變化、離極限多遠（不用學）。
peer  ：跟兄弟比，同一個服務的 Pod 互相比，抓只有一個 Pod 壞掉（不用學）。
feedback：值班對每筆異常的判斷，統計誤報率；誤報累積多次才提放寬門檻的建議（proposed，人核准才生效）。
incident：事故紀錄（時間、服務、根因、處置）；--exclude-training 同時提一筆「這段不學」（proposed）。
chronic：慢性病清單（天天噴錯、沒人修的前 10 名，附負責人），每週一次；寫 aiops-chronic。
quality：資料品質檢查（路由沒參數化、必填欄位缺、資料停了、落到 unassigned 變多），餵給 baseline 之前先看資料本身。
--end、--at 用來回測（拿過去某個時間點當成「現在」）；不給就是現在。
"""
from __future__ import annotations

import argparse
import sys

import yaml

from .cmd_baseline import cmd_train, cmd_tune, cmd_detect
from .cmd_adjust import cmd_adjust
from .cmd_backtest import cmd_backtest
from .cmd_metrics import cmd_metrics
from .cmd_feedback import cmd_feedback, cmd_incident
from .cmd_chronic import cmd_chronic
from .cmd_prune import cmd_prune
from .cmd_trace import cmd_trace_collect, cmd_trace_train, cmd_trace_detect, cmd_trace_merge
from .cmd_rules import cmd_rules
from .cmd_peer import cmd_peer
from .cmd_quality import cmd_quality


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="detector")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--config", required=True)
    t.add_argument("--weeks", type=int)
    t.add_argument("--end")
    d = sub.add_parser("detect")
    d.add_argument("--config", required=True)
    d.add_argument("--window", default="30min")
    d.add_argument("--at")
    tu = sub.add_parser("tune", help="調整某條序列、某些對象的粒度、z、min_abs（寫進 aiops-tuning）")
    tu.add_argument("--config", default="configs/baseline/series.yaml")
    tu.add_argument("--series", help="序列名稱（可用正規表示式，例如 http\\..*）")
    tu.add_argument("--match", nargs="*", help="對象，例如 service=^mes- route=/lots（正規表示式）")
    tu.add_argument("--resolution", help="固定粒度，例如 5m")
    tu.add_argument("--z", type=float)
    tu.add_argument("--min-abs", dest="min_abs", type=float)
    tu.add_argument("--confirm-points", dest="confirm_points", type=int, help="連續幾個時間窗超標才算 anomaly（1 = 一次就算）")
    tu.add_argument("--instant-z", dest="instant_z", type=float, help="偏離幾倍就不用等連續，直接算 anomaly")
    tu.add_argument("--reason", help="為什麼調（例如：誤報多、月底批次）")
    tu.add_argument("--by", help="誰調的（使用者、feedback）")
    tu.add_argument("--remove", action="store_true")
    tu.add_argument("--list", action="store_true")
    tu.add_argument("--show", action="store_true",
                    help="看最終生效的值與來源：--series 給 series.yaml 的名稱、--match 給對象的實際值（例如 service=mes-api），不寫入")
    bt = sub.add_parser("backtest", help="改門檻前先回測：一筆建議用過去 N 週重跑，看告警少幾個、已確認事故還抓不抓得到")
    bt.add_argument("--config", default="configs/baseline/series.yaml")
    bt.add_argument("--id", required=True, help="aiops-tuning 的建議 id（adjust list 看得到）")
    bt.add_argument("--weeks", type=int, default=4)
    bt.add_argument("--at")
    bt.add_argument("--verbose", action="store_true")
    ad = sub.add_parser("adjust", help="排除時段、重置點、週期放寬（寫進 aiops-tuning）")
    ad.add_argument("action", choices=["add", "list", "approve", "remove"])
    ad.add_argument("--config", default="configs/baseline/series.yaml")
    ad.add_argument("--kind", choices=["exclude_window", "reset_point", "recurring_override"], default="exclude_window")
    ad.add_argument("--id", help="指定 id（同 id 再寫會覆蓋）；approve、remove 必填")
    ad.add_argument("--series", help="哪些序列（正規表示式，不給就是全部）")
    ad.add_argument("--match", nargs="*", help="對象，例如 service=^mes-api$（正規表示式；欄位可用別名：service、namespace、pod…）")
    ad.add_argument("--start", help="exclude_window：開始（ISO 時間，例如 2026-10-20T02:00:00+08:00）")
    ad.add_argument("--end", help="exclude_window：結束")
    ad.add_argument("--apply-to", dest="apply_to", default="train", help="exclude_window：train（不學，預設）、train,detect（也不報）")
    ad.add_argument("--at", help="reset_point：從這個時間點起重新學")
    ad.add_argument("--warmup", help="reset_point：重置後多久內不判斷（預設 24h）")
    ad.add_argument("--cron", help="recurring_override：開始時間（5 欄 cron，台北時間），例如 '0 20 * * *'")
    ad.add_argument("--duration", help="recurring_override：持續多久，例如 4h")
    ad.add_argument("--days-of-month", dest="days_of_month", help="recurring_override：哪幾天，-1 是每月最後一天（逗號分開）")
    ad.add_argument("--z", type=float, help="recurring_override：這段時間 z 至少要多少才算")
    ad.add_argument("--min-abs", dest="min_abs", type=float, help="recurring_override：這段時間至少差多少才算")
    ad.add_argument("--mute", action="store_true", help="recurring_override：這段時間不報")
    ad.add_argument("--train", choices=["keep", "exclude"], help="recurring_override：這段時間要不要學（預設 keep）")
    ad.add_argument("--status", choices=["active", "proposed"], default="active")
    ad.add_argument("--source", default="manual", help="manual、feedback、cicd")
    ad.add_argument("--expires", help="到期時間（選填）")
    ad.add_argument("--reason")
    ad.add_argument("--by")
    ad.add_argument("--force", action="store_true", help="approve：沒回測、或回測不建議也硬核准")
    tc = sub.add_parser("trace-collect")
    tc.add_argument("--config", required=True)
    tc.add_argument("--start")
    tc.add_argument("--end")
    tt = sub.add_parser("trace-train")
    tt.add_argument("--config", required=True)
    tt.add_argument("--history", help="學多久的資料，例如 4w、2h（預設 history_weeks）")
    tt.add_argument("--end")
    td = sub.add_parser("trace-detect")
    td.add_argument("--config", required=True)
    td.add_argument("--window", default="30min")
    td.add_argument("--at")
    tm = sub.add_parser("trace-merge")
    tm.add_argument("--config", required=True)
    tm.add_argument("--window", default="3min", help="第一次（沒有上次合到哪的紀錄）往前合多久")
    tm.add_argument("--at")
    r = sub.add_parser("rules")
    r.add_argument("--config", required=True)
    r.add_argument("--at", help="回測：拿過去某個時間點當成現在")
    r.add_argument("--only", help="只跑這幾條（逗號分開）")
    pe = sub.add_parser("peer")
    pe.add_argument("--config", required=True)
    pe.add_argument("--at", help="回測：拿過去某個時間點當成現在")
    pe.add_argument("--only", help="只跑這幾個（逗號分開）")
    fb = sub.add_parser("feedback", help="值班回饋：pending（還沒標記的）、add、list、summary（誤報統計）、suggest（誤報多次→建議放寬）")
    fb.add_argument("action", choices=["pending", "add", "list", "summary", "suggest"])
    fb.add_argument("--config", default="configs/baseline/series.yaml")
    fb.add_argument("--anomaly", help="add：異常的 id（pending 看得到）")
    fb.add_argument("--verdict", help="add：true_positive（真的）、false_positive（誤報）、wrong_root_cause（根因錯）、missed（漏報）")
    fb.add_argument("--note")
    fb.add_argument("--by")
    fb.add_argument("--incident", help="add：屬於哪件事故")
    fb.add_argument("--alert", help="add：屬於哪件告警（aiops-alerts 的 alert_id；--text 抽取時的服務、序列從它帶入）")
    fb.add_argument("--text", help="add：一句自然語言（例：這是每月初的批次匯出報表，屬於預期內負載）；抽成四欄、對得到的提一筆 proposed 的調整；"
                                   "沒給 --verdict 就用抽出來的")
    fb.add_argument("--root-cause", dest="root_cause", help="add：正確的根因（wrong_root_cause 時）")
    fb.add_argument("--series", help="add missed：哪條序列；pending：序列名稱包含")
    fb.add_argument("--match", nargs="*", help="add missed：對象，例如 service=mes-api")
    fb.add_argument("--at", help="add missed：漏報的時間")
    fb.add_argument("--days", type=int, default=28)
    fb.add_argument("--limit", type=int, default=50)
    fb.add_argument("--min-false-positive", dest="min_false_positive", type=int, default=3,
                    help="同一個對象誤報幾次才列入、才提建議（預設 3）")
    fb.add_argument("--min-recurring", dest="min_recurring", type=int, default=3,
                    help="suggest：同一個服務、同一個根因分類確認幾次才提「升級成規則」（預設 3）")
    me = sub.add_parser("metrics", help="量化：每天幾件、有回饋的比例、誤報率、從告警到回饋多久；兩段時間並排比「學之前 vs 學之後」")
    me.add_argument("--config", default="configs/baseline/series.yaml")
    me.add_argument("--from", dest="start", required=True, help="例如 2026-10-01")
    me.add_argument("--to", dest="end", required=True)
    me.add_argument("--compare-from", dest="cstart", help="另一段（學之前）")
    me.add_argument("--compare-to", dest="cend")
    me.add_argument("--out")
    inc = sub.add_parser("incident", help="事故紀錄：add、list、resolve")
    inc.add_argument("action", choices=["add", "list", "resolve"])
    inc.add_argument("--config", default="configs/baseline/series.yaml")
    inc.add_argument("--id")
    inc.add_argument("--title")
    inc.add_argument("--start")
    inc.add_argument("--end")
    inc.add_argument("--service", nargs="*", help="影響的服務")
    inc.add_argument("--severity", help="例如 sev1、sev2")
    inc.add_argument("--root-cause", dest="root_cause")
    inc.add_argument("--action-taken", dest="action_taken", help="處置")
    inc.add_argument("--anomaly", nargs="*", help="相關的異常 id")
    inc.add_argument("--by")
    inc.add_argument("--exclude-training", dest="exclude_training", action="store_true",
                     help="同時提一筆「這段時間不學」的建議（proposed，adjust approve 才生效）")
    ch = sub.add_parser("chronic", help="慢性病清單：天天噴錯、沒人修的（每週）")
    ch.add_argument("--config", required=True)
    ch.add_argument("--at", help="回測：拿過去某個時間點當成現在")
    q = sub.add_parser("quality")
    q.add_argument("--config", required=True)
    q.add_argument("--at", help="回測：拿過去某個時間點當成現在")
    q.add_argument("--only", help="只跑這幾個（逗號分開）")
    pr = sub.add_parser("prune", help="aiops-anomalies、aiops-trace-segments、已結案的 aiops-alerts 超過保留天數的文件清掉（每天）")
    pr.add_argument("--config", required=True)
    pr.add_argument("--at", help="拿某個時間點當成現在")
    pr.add_argument("--dry-run", dest="dry_run", action="store_true", help="只印會刪幾筆，不刪")
    args = p.parse_args(argv)
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cmds = {"train": cmd_train, "detect": cmd_detect, "tune": cmd_tune, "trace-collect": cmd_trace_collect,
            "trace-train": cmd_trace_train, "trace-detect": cmd_trace_detect, "trace-merge": cmd_trace_merge, "rules": cmd_rules, "peer": cmd_peer,
            "quality": cmd_quality, "adjust": cmd_adjust, "backtest": cmd_backtest, "metrics": cmd_metrics,
            "chronic": cmd_chronic, "feedback": cmd_feedback, "incident": cmd_incident, "prune": cmd_prune}
    return cmds[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())

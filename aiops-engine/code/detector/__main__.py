"""用法（在 engine 底下；每個指令的程式在各功能的資料夾（baseline/、rules/、adjust/、feedback/、prune.py），這裡只有參數與分派）：

  python3 -m detector train  --config configs/baseline/series.yaml [--weeks 4] [--end 2026-10-10T00:00:00Z]
  python3 -m detector detect --config configs/baseline/series.yaml [--window 30m] [--at 2026-10-10T08:00:00Z]
  python3 -m detector tune   --series http.latency_p95_s --match service=mes-api --resolution 5m --reason 誤報多
  python3 -m detector adjust add --kind exclude_window --match service=mes-api --start … --end … --reason 事故
  python3 -m detector adjust list | approve --id … | remove --id …
  python3 -m detector rules  --config configs/baseline/rules.yaml  [--at …] [--only 規則,…]
  python3 -m detector peer   --config configs/baseline/peer.yaml   [--at …] [--only 檢查,…]
  python3 -m detector quality --config configs/baseline/quality.yaml [--at …] [--only 檢查,…]
  python3 -m detector chronic --config configs/baseline/chronic.yaml [--at …]
  python3 -m detector feedback pending|add|list|summary|suggest|export   值班回饋（真的／誤報／根因錯／漏報）；export 匯出給 ML 團隊
  python3 -m detector incident add|list|resolve                   事故紀錄
  python3 -m detector ingest --config configs/factory/ingest.yaml [--window 10m] [--at …]     別人判好的異常（ML、FDC）接進 ⑤
  （情境的子命令由 plugin 的 commands() 給、只在那個 plugin 下才有：observability 有 peer、quality、chronic、trace-*；
   factory 有 ingest、paths-merge；metrics_only 有 ingest。core/plugin.py、plugins/README.md）

命令列用 typer：每個子命令是一個函式，參數就是命令列參數；@command 登記進 COMMANDS，main() 建 app 時連同
plugin.commands() 一起掛上（兩邊同一種形狀：{子命令名: register(app)}）。函式只把參數裝成 argparse.Namespace、
用 core.config.load 讀設定，再交給各功能的 cmd_*(cfg, args)（detector/cli.py 的 run）。
可以給好幾個值的參數（--match、--service、--anomaly、--id）要重複寫：--match service=a --match route=b。
旗標字串是對外契約（排程表、文件都寫 --service）；Python 參數名用中立的 entity，旗標明寫在 Opt 裡。

train ：讀過去 N 週的資料，每條序列、每個時段學出中位數與波動範圍，寫進 ES 的 aiops-baseline。
detect：讀最近一段時間的資料，跟 baseline 比，超出範圍的寫進 aiops-anomalies，並印出來。
        跳一次是 warning，同一條序列連續兩個時間窗超標（或連續超標滿 30 分鐘）才是 anomaly。
        預設 30 秒一點（跟收值一樣），每 30 秒跑一次：連續兩個 30 秒（約 1 分鐘）就是 anomaly。
tune  ：調整某條序列、某些對象的粒度、z、min_abs（寫進 aiops-tuning；之後 feedback 也寫這裡），下次 train／detect 生效。
        --show 看某條序列、某個對象現在生效的值是多少、來自哪一筆（多筆都符合時後面蓋前面）。
adjust：排除時段（事故、維護、壓測不學）、重置點（換版後只學新的）、週期放寬（月底批次放寬或不報），
        也寫 aiops-tuning（detector/adjust/adjust.py）。status: proposed 的要 approve 才生效。
rules ：狀態變化、離極限多遠（不用學）。
peer  ：跟兄弟比，同一個服務的 Pod 互相比，抓只有一個 Pod 壞掉（不用學）。
feedback：值班對每筆異常的判斷，統計誤報率；誤報累積多次才提放寬門檻的建議（proposed，人核准才生效）。
incident：事故紀錄（時間、服務、根因、處置）；--exclude-training 同時提一筆「這段不學」（proposed）。
chronic：慢性病清單（天天噴錯、沒人修的前 10 名，附負責人），每週一次；寫 aiops-chronic。
quality：資料品質檢查（路由沒參數化、必填欄位缺、資料停了、落到 unassigned 變多），餵給 baseline 之前先看資料本身。
--end、--at 用來回測（拿過去某個時間點當成「現在」）；不給就是現在。
"""
from __future__ import annotations

import sys
from typing import Callable, List, Literal

import typer

from core import plugin

from .adjust.backtest import cmd_backtest
from .adjust.command import cmd_adjust
from .adjust.shift import cmd_shift
from .baseline.config import SeriesConfig
from .baseline.detect import cmd_detect
from .baseline.train import cmd_train
from .baseline.tune import cmd_tune
from .cli import Opt, run
from .feedback.command import cmd_feedback, cmd_incident
from .feedback.metrics import cmd_metrics
from .prune import PruneConfig, cmd_prune
from .rules.command import cmd_rules
from .rules.config import RulesConfig

SERIES = "configs/baseline/series.yaml"
COMMANDS: dict[str, Callable[[typer.Typer], None]] = {}      # 內建子命令：名字 → register(app)；plugin.commands() 回同一種形狀


def command(name: str, help: str | None = None):
    """登記一個內建子命令：被裝飾的函式就是命令，參數就是命令列參數。"""
    def deco(fn):
        COMMANDS[name] = lambda app: app.command(name, help=help)(fn)
        return fn
    return deco


@command("train")
def train(config: str = Opt(...), weeks: int | None = Opt(None), end: str | None = Opt(None)) -> int:
    return run(cmd_train, SeriesConfig, locals())


@command("detect")
def detect(config: str = Opt(...), window: str = Opt("30min"), at: str | None = Opt(None)) -> int:
    return run(cmd_detect, SeriesConfig, locals())


@command("tune", help="調整某條序列、某些對象的粒度、z、min_abs（寫進 aiops-tuning）")
def tune(config: str = Opt(SERIES),
         series: str | None = Opt(None, help="序列名稱（可用正規表示式，例如 http\\..*）"),
         match: List[str] | None = Opt(None, help="對象，例如 --match service=^mes- --match route=/lots（正規表示式）"),
         resolution: str | None = Opt(None, help="固定粒度，例如 5m"),
         z: float | None = Opt(None, "--z"),
         min_abs: float | None = Opt(None),
         confirm_points: int | None = Opt(None, help="連續幾個時間窗超標才算 anomaly（1 = 一次就算）"),
         instant_z: float | None = Opt(None, help="偏離幾倍就不用等連續，直接算 anomaly"),
         reason: str | None = Opt(None, help="為什麼調（例如：誤報多、月底批次）"),
         by: str | None = Opt(None, help="誰調的（使用者、feedback）"),
         remove: bool = Opt(False, "--remove"),
         list_: bool = Opt(False, "--list"),
         show: bool = Opt(False, "--show", help="看最終生效的值與來源：--series 給 series.yaml 的名稱、--match 給對象的實際值（例如 service=mes-api），不寫入")) -> int:
    return run(cmd_tune, SeriesConfig, locals())


@command("backtest", help="改門檻前先回測：一筆或一組建議用過去 N 週重跑，看告警少幾個、已確認事故還抓不抓得到；一組有效才逐筆剔除看哪筆多餘")
def backtest(config: str = Opt(SERIES),
             id_: List[str] = Opt(..., "--id", help="aiops-tuning 的建議 id（adjust list 看得到）；給好幾個（--id a --id b）就一組一起測"),
             group: bool = Opt(False, "--group", help="把同一個對象（同序列、同 match）其他還是 proposed 的建議一起拉進來當一組"),
             weeks: int = Opt(4), at: str | None = Opt(None), verbose: bool = Opt(False, "--verbose")) -> int:
    return run(cmd_backtest, SeriesConfig, locals())


@command("adjust", help="排除時段、重置點、週期放寬（寫進 aiops-tuning）")
def adjust(action: Literal["add", "list", "approve", "remove"] = typer.Argument(...),
           config: str = Opt(SERIES),
           kind: Literal["exclude_window", "reset_point", "recurring_override"] = Opt("exclude_window"),
           id_: str | None = Opt(None, "--id", help="指定 id（同 id 再寫會覆蓋）；approve、remove 必填"),
           series: str | None = Opt(None, help="哪些序列（正規表示式，不給就是全部）"),
           match: List[str] | None = Opt(None, help="對象，例如 --match service=^mes-api$（正規表示式；欄位可用別名：service、namespace、pod…）"),
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
           reason: str | None = Opt(None), by: str | None = Opt(None),
           force: bool = Opt(False, "--force", help="approve：沒回測、或回測不建議也硬核准")) -> int:
    return run(cmd_adjust, SeriesConfig, locals())


@command("rules")
def rules(config: str = Opt(...), at: str | None = Opt(None, help="回測：拿過去某個時間點當成現在"),
          only: str | None = Opt(None, help="只跑這幾條（逗號分開）")) -> int:
    return run(cmd_rules, RulesConfig, locals())


@command("feedback", help="值班回饋：pending（還沒標記的）、add、list、summary（誤報統計）、suggest（誤報多次→建議放寬）、"
                          "export（回饋＋異常匯出成 NDJSON／CSV，給 ML 團隊重訓；工廠版）")
def feedback(action: Literal["pending", "add", "list", "summary", "suggest", "export"] = typer.Argument(...),
             out: str | None = Opt(None, help="export：寫到哪個檔（.ndjson 或 .csv；不給就印到 stdout）"),
             config: str = Opt(SERIES),
             anomaly: str | None = Opt(None, help="add：異常的 id（pending 看得到）"),
             verdict: str | None = Opt(None, help="add：true_positive（真的）、false_positive（誤報）、wrong_root_cause（根因錯）、missed（漏報）"),
             note: str | None = Opt(None), by: str | None = Opt(None),
             incident: str | None = Opt(None, help="add：屬於哪件事故"),
             alert: str | None = Opt(None, help="add：屬於哪件告警（aiops-alerts 的 alert_id；--text 抽取時的服務、序列從它帶入）"),
             text: str | None = Opt(None, help="add：一句自然語言（例：這是每月初的批次匯出報表，屬於預期內負載）；抽成四欄、對得到的提一筆 proposed 的調整；"
                                              "沒給 --verdict 就用抽出來的"),
             root_cause: str | None = Opt(None, help="add：正確的根因（wrong_root_cause 時）"),
             series: str | None = Opt(None, help="add missed：哪條序列；pending：序列名稱包含"),
             match: List[str] | None = Opt(None, help="add missed：對象，例如 --match service=mes-api"),
             at: str | None = Opt(None, help="add missed：漏報的時間"),
             days: int = Opt(28), limit: int = Opt(50),
             min_false_positive: int = Opt(1, help="同一個對象誤報幾次才列入、才提建議（預設 1：有人標就提，不累積）"),
             min_recurring: int = Opt(3, help="suggest：同一個服務、同一個根因分類確認幾次才提「升級成規則」（預設 3）")) -> int:
    return run(cmd_feedback, SeriesConfig, locals())


@command("metrics", help="量化：每天幾件、有回饋的比例、誤報率、從告警到回饋多久；兩段時間並排比「學之前 vs 學之後」")
def metrics(config: str = Opt(SERIES),
            start: str = Opt(..., "--from", help="例如 2026-10-01"), end: str = Opt(..., "--to"),
            cstart: str | None = Opt(None, "--compare-from", help="另一段（學之前）"), cend: str | None = Opt(None, "--compare-to"),
            out: str | None = Opt(None)) -> int:
    return run(cmd_metrics, SeriesConfig, locals())


@command("incident", help="事故紀錄：add、list、resolve")
def incident(action: Literal["add", "list", "resolve"] = typer.Argument(...),
             config: str = Opt(SERIES), id_: str | None = Opt(None, "--id"),
             title: str | None = Opt(None), start: str | None = Opt(None), end: str | None = Opt(None),
             entity: List[str] | None = Opt(None, "--service", help="影響的服務（--service a --service b）"),
             severity: str | None = Opt(None, help="例如 sev1、sev2"),
             root_cause: str | None = Opt(None), action_taken: str | None = Opt(None, help="處置"),
             anomaly: List[str] | None = Opt(None, help="相關的異常 id"), by: str | None = Opt(None),
             exclude_training: bool = Opt(False, "--exclude-training", help="同時提一筆「這段時間不學」的建議（proposed，adjust approve 才生效）")) -> int:
    return run(cmd_incident, SeriesConfig, locals())


@command("prune", help="aiops-anomalies、aiops-trace-segments、已結案的 aiops-alerts 超過保留天數的文件清掉（每天）")
def prune(config: str = Opt(...), at: str | None = Opt(None, help="拿某個時間點當成現在"),
          dry_run: bool = Opt(False, "--dry-run", help="只印會刪幾筆，不刪")) -> int:
    return run(cmd_prune, PruneConfig, locals())


@command("shift", help="一階跳變：最近一天幾乎每點都往同一邊超標、新水準很穩的序列，提 reset_point（proposed）")
def shift(config: str = Opt(..., help="series.yaml（可加 shift: {window, min_points, min_share, max_spread}）"),
          at: str | None = Opt(None, help="拿某個時間點當成現在")) -> int:
    return run(cmd_shift, SeriesConfig, locals())


def build_app() -> typer.Typer:
    """內建子命令＋現在這個 plugin 多出來的（例如工廠的 paths-merge）。每次 main 重建：測試會換 AIOPS_PLUGIN。"""
    app = typer.Typer(add_completion=False, rich_markup_mode=None, pretty_exceptions_enable=False)
    for register in {**COMMANDS, **plugin.current().commands()}.values():
        register(app)
    return app


def main(argv=None) -> int:
    try:
        rv = build_app()(args=argv, prog_name="detector", standalone_mode=False)
    except typer.TyperException as e:          # 參數錯（沒這個子命令、少 --config、choices 不對）：印用法、回 2，跟 argparse 一樣
        if not hasattr(e, "exit_code"):
            raise
        e.show()
        return e.exit_code
    return int(rv or 0)


if __name__ == "__main__":
    sys.exit(main())

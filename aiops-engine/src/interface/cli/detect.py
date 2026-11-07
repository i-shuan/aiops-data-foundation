"""用法（在 engine 底下；每個指令的程式在 detector/cmd_*.py，這裡只有參數與分派）：

  python3 -m main detector train  --config configs/baseline/series.yaml [--weeks 4] [--end 2026-10-10T00:00:00Z]
  python3 -m main detector detect --config configs/baseline/series.yaml [--window 30m] [--at 2026-10-10T08:00:00Z]
  python3 -m main detector tune   --series http.latency_p95_s --match service=mes-api --resolution 5m --reason 誤報多
  python3 -m main detector adjust add --kind exclude_window --match service=mes-api --start … --end … --reason 事故
  python3 -m main detector adjust list | approve --id … | remove --id …
  python3 -m main detector rules  --config configs/baseline/rules.yaml  [--at …] [--only 規則,…]
  python3 -m main detector peer   --config configs/baseline/peer.yaml   [--at …] [--only 檢查,…]
  python3 -m main detector quality --config configs/baseline/quality.yaml [--at …] [--only 檢查,…]
  python3 -m main detector chronic --config configs/baseline/chronic.yaml [--at …]
  python3 -m main detector feedback pending|add|list|summary|suggest|export   值班回饋（真的／誤報／根因錯／漏報）；export 匯出給 ML 團隊
  python3 -m main detector incident add|list|resolve                   事故紀錄
  python3 -m main detector ingest --config configs/factory/ingest.yaml [--window 10m] [--at …]     別人判好的異常（ML、FDC）接進 ⑤
  （情境加的子命令只在那個情境下才有：AIOPS_PLUGIN=factory 多 paths-merge；common/scenario/factory.yaml 的 commands；common/scenario/README.md）

命令列用 typer：每個子命令是一個函式，參數就是命令列參數；@command 登記進 COMMANDS，main() 建 app 時連同
feedback/cli.py（⑨：feedback、incident、adjust、backtest、metrics）和 Scenario.commands 一起掛上（三邊同一種形狀：{子命令名: register(app)}）。
函式只把參數裝成 argparse.Namespace、用 core.config.load_config 讀設定，再交給 cmd_*(config, args)（core/cli.py 的 run）。
這個檔是入口（組合根）：它可以 import feedback/，feedback/ 不會 import 它。
可以給好幾個值的參數（--match、--service、--anomaly、--id）要重複寫：--match service=a --match route=b。

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

import sys
from typing import Callable, List

import pandas as pd
import typer

from common.clock import now_of
from common.infra.es import ES
from common.infra.repos import Repos
from common.scenario import load_scenario
from common.config import load_config
from data.source import ADAPTERS, for_index
from data.source.check import check_trace_index
from data.trace import span_merge
from detect.detect_service import run_source_check, source_findings
from interface.cli.common import SERIES, Opt, at_opt, registry, run, window_opt
import interface.cli.feedback as feedback_cli

from detect.detect_service import cmd_train, cmd_tune, cmd_detect
from detect.strategy.chronic_issue import cmd_chronic
from detect.housekeeping import cmd_prune
from detect.series_ops.level_shift import cmd_shift
from detect.trace.latency_stats import cmd_trace_collect, cmd_trace_train, cmd_trace_detect
from detect.strategy.threshold_rule import cmd_rules
from detect.strategy.peer_compare import cmd_peer
from detect.series_ops.quality_check import cmd_quality
from interface.cli.fab import cmd_ingest

COMMANDS: dict[str, Callable[[typer.Typer], None]] = {}      # 內建子命令：名字 → register(app)；feedback.cli.COMMANDS、plugin.commands() 同一種形狀
command = registry(COMMANDS)


@command("train")
def train(config: str = Opt(...), weeks: int | None = Opt(None), end: pd.Timestamp | None = at_opt()) -> int:
    return run(cmd_train, "series", config=config, weeks=weeks, end=end)


@command("detect")
def detect(config: str = Opt(...), window: pd.Timedelta = window_opt("30m"), at: pd.Timestamp | None = at_opt()) -> int:
    return run(cmd_detect, "series", config=config, window=window, at=at)


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
    return run(cmd_tune, "series", config=config, series=series, match=match, resolution=resolution, z=z, min_abs=min_abs, confirm_points=confirm_points, instant_z=instant_z, reason=reason, by=by, remove=remove, list_=list_, show=show)


@command("trace-collect")
def trace_collect(config: str = Opt(...), start: pd.Timestamp | None = at_opt(), end: pd.Timestamp | None = at_opt()) -> int:
    return run(cmd_trace_collect, "trace", config=config, start=start, end=end)


@command("trace-train")
def trace_train(config: str = Opt(...), history: str | None = Opt(None, help="學多久的資料，例如 4w、2h（預設 history_weeks）"),
                end: pd.Timestamp | None = at_opt()) -> int:
    return run(cmd_trace_train, "trace", config=config, history=history, end=end)


@command("trace-detect")
def trace_detect(config: str = Opt(...), window: pd.Timedelta = window_opt("30m"), at: pd.Timestamp | None = at_opt()) -> int:
    return run(cmd_trace_detect, "trace", config=config, window=window, at=at)


def cmd_trace_merge(trace_config, args) -> int:
    """raw（每一段一筆）→ 一個請求一筆（traces-merged-<group>-<日期>）。排程器每分鐘跑；從上次合到的時間接著合，中斷很久會分段補。
    合成本身在 data/trace/span_merge（② 清資料，不認得 trace.yaml 的設定檔）；這裡只把設定裡的 raw_index、settle_seconds 交給它。"""
    repo = Repos(ES(), load_scenario())
    at = now_of(args.at).to_pydatetime()
    run_source_check(repo, "trace", now_of(args.at),         # 宣告的來源跟 raw trace 對得上嗎：只回報，不擋合成
                     lambda: check_trace_index(repo.es, trace_config.raw_index, for_index(trace_config.raw_index, repo.scenario.source), ADAPTERS))
    since, until, n = span_merge.run(repo, trace_config.raw_index, trace_config.merge.settle_seconds, at, args.window.to_pytimedelta())
    print(f"trace-merge {since:%H:%M:%S}～{until:%H:%M:%S}：合了 {n} 個 trace")
    return 0


def cmd_source_check(series_config, args) -> int:
    """宣告的來源（AIOPS_SOURCE）跟 ES 裡真的資料對得上嗎：每個序列的索引與欄位、（有給 --trace-config 時）raw trace 的格式。
    只回報、永遠回 0（要看有沒有問題看輸出；排程裡每輪也會自動檢查並寫狀態文件，Grafana 叫人）。"""
    repo = Repos(ES(), load_scenario())
    found = run_source_check(repo, "series", now_of(None), lambda: source_findings(repo.es, series_config, repo.scenario.owner))
    if args.trace_config:
        tc = load_config(args.trace_config, "trace")
        found += run_source_check(repo, "trace", now_of(None), lambda: check_trace_index(repo.es, tc.raw_index, for_index(tc.raw_index, repo.scenario.source), ADAPTERS))
    print(f"來源自我檢查：宣告 {repo.scenario.source}，{'全部對得上' if not any(f.counts for f in found) else str(sum(f.counts for f in found)) + ' 個地方對不上（上面 ⚠ 的）'}；{sum(not f.counts for f in found)} 個只是抽不到資料（ℹ）")
    return 0


@command("source-check", help="宣告的來源（AIOPS_SOURCE）跟 ES 裡的資料對得上嗎（只回報，不擋）")
def source_check(config: str = Opt(..., help="series.yaml"), trace_config: str | None = Opt(None, help="trace.yaml：一併檢查 raw trace 的格式")) -> int:
    return run(cmd_source_check, "series", config=config, trace_config=trace_config)


@command("trace-merge")
def trace_merge(config: str = Opt(...), window: pd.Timedelta = window_opt("3m", "第一次（沒有上次合到哪的紀錄）往前合多久"),
                at: pd.Timestamp | None = at_opt()) -> int:
    return run(cmd_trace_merge, "trace", config=config, window=window, at=at)


@command("rules")
def rules(config: str = Opt(...), at: pd.Timestamp | None = at_opt("回測：拿過去某個時間點當成現在"),
          only: str | None = Opt(None, help="只跑這幾條（逗號分開）")) -> int:
    return run(cmd_rules, "rules", config=config, at=at, only=only)


@command("peer")
def peer(config: str = Opt(...), at: pd.Timestamp | None = at_opt("回測：拿過去某個時間點當成現在"),
         only: str | None = Opt(None, help="只跑這幾個（逗號分開）")) -> int:
    return run(cmd_peer, "peer", config=config, at=at, only=only)


@command("chronic", help="慢性病清單：天天噴錯、沒人修的（每週）")
def chronic(config: str = Opt(...), at: pd.Timestamp | None = at_opt("回測：拿過去某個時間點當成現在")) -> int:
    return run(cmd_chronic, "chronic", config=config, at=at)


@command("quality")
def quality(config: str = Opt(...), at: pd.Timestamp | None = at_opt("回測：拿過去某個時間點當成現在"),
            only: str | None = Opt(None, help="只跑這幾個（逗號分開）")) -> int:
    return run(cmd_quality, "quality", config=config, at=at, only=only)


@command("prune", help="aiops-anomalies、aiops-trace-segments、已結案的 aiops-alerts 超過保留天數的文件清掉（每天）")
def prune(config: str = Opt(...), at: pd.Timestamp | None = at_opt("拿某個時間點當成現在"),
          dry_run: bool = Opt(False, "--dry-run", help="只印會刪幾筆，不刪")) -> int:
    return run(cmd_prune, "prune", config=config, at=at, dry_run=dry_run)


@command("shift", help="一階跳變：最近一天幾乎每點都往同一邊超標、新水準很穩的序列，提 reset_point（proposed）")
def shift(config: str = Opt(..., help="series.yaml（可加 shift: {window, min_points, min_share, max_spread}）"),
          at: pd.Timestamp | None = at_opt("拿某個時間點當成現在")) -> int:
    return run(cmd_shift, "series", config=config, at=at)


@command("ingest", help="別人判好的異常（ML 的異常跳點、FDC 鳴叫）→ aiops-anomalies（detector/ingest.py）")
def ingest(config: str = Opt(..., help="configs/factory/ingest.yaml"),
           window: pd.Timedelta = window_opt("10m", "看最近多久的來源列（預設 10m；_id 依來源識別碼，重讀不重複）"),
           at: pd.Timestamp | None = at_opt("拿某個時間點當成現在")) -> int:
    return run(cmd_ingest, "ingest", config=config, window=window, at=at)


def build_app() -> typer.Typer:
    """detector 的子命令＋⑨ 的（feedback/cli.py）＋現在這個情境多出來的（例如工廠的 paths-merge）。每次 main 重建：測試會換 AIOPS_PLUGIN。"""
    app = typer.Typer(add_completion=False, rich_markup_mode=None, pretty_exceptions_enable=False)
    for register in {**COMMANDS, **feedback_cli.COMMANDS, **load_scenario().commands}.values():
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

"""prune：engine 自己的資料（PostgreSQL）超過保留天數的每天清一次（configs/baseline/prune.yaml）。

清哪些、依什麼時間由各 repository 的 prune(cutoff) 決定：異常、trace 每段的統計依自己的時間；告警只刪已結案的、依結案時間
（開著的件、狀態文件不會被刪）。人工判斷的事故、回饋、調整不在這裡：事故紀錄要長留，掉了不能重算（備份交給 DBA 的 PostgreSQL 備份）。
"""
from __future__ import annotations

import pandas as pd

from common.clock import now_of
from common.infra.es import ES
from common.infra.repos import Repos
from common.scenario import load_scenario
from detect.config.prune import PruneConfig


def plan(prune_config: PruneConfig, now: pd.Timestamp) -> list[dict]:
    """每種資料要刪多早以前的：{"table", "keep_days", "cutoff"}。"""
    return [{"table": t.table, "keep_days": float(t.keep_days), "cutoff": (now - pd.Timedelta(days=float(t.keep_days))).to_pydatetime()}
            for t in prune_config.tables]


def cmd_prune(prune_config: PruneConfig, args, repo: Repos | None = None) -> int:
    repo = repo or Repos(ES(), load_scenario())
    now = now_of(args.at)
    dry = bool(getattr(args, "dry_run", False))
    total = 0
    for p in plan(prune_config, now):
        r = getattr(repo, p["table"])
        if dry:
            n = r.prune_count(p["cutoff"])
            print(f"  {p['table']}：早於 {p['cutoff']:%Y-%m-%d %H:%M}（保留 {p['keep_days']:g} 天）的有 {n} 筆" + ("，沒刪（--dry-run）" if n else ""))
        else:
            n = r.prune(p["cutoff"])
            print(f"  {p['table']}：刪了 {n} 筆（早於 {p['cutoff']:%Y-%m-%d %H:%M}，保留 {p['keep_days']:g} 天）")
        total += n
    print(f"{now.tz_convert(prune_config.timezone):%m-%d %H:%M}：共 {total} 筆" + ("要刪" if dry else ""))
    return 0

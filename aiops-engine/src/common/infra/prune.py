"""prune：engine 自己的資料（PostgreSQL）超過保留天數的每天處理一次（configs/baseline/prune.yaml）。

清哪些、依什麼時間由各 repository 的 PRUNE_WHERE 決定：異常、trace 每段的統計依自己的時間；告警只處理已結案的、依結案時間
（開著的件、狀態文件不會動）。人工判斷的事故、回饋、調整不在這裡：事故紀錄要長留，掉了不能重算（備份交給 DBA 的 PostgreSQL 備份）。

超過保留天數的不是直接刪：一批一批「讀 → 一天打包成一筆 gzip 寫進 aiops_packed（common/infra/pack.py）→ 才刪細筆」，
所以寫入失敗那一批不會刪；打包的資料再留 pack.keep_days 天。prune.yaml 寫 pack: null 就直接刪。
"""
from __future__ import annotations

import pandas as pd

from common.clock import now_of
from common.config.prune import PruneConfig, PrunePack
from common.infra.repos import Repos
from common.scenario import load_scenario


def plan(prune_config: PruneConfig, now: pd.Timestamp) -> list[dict]:
    """每種資料要處理多早以前的：{"table", "keep_days", "cutoff"}。"""
    return [{"table": t.table, "keep_days": float(t.keep_days), "cutoff": (now - pd.Timedelta(days=float(t.keep_days))).to_pydatetime()}
            for t in prune_config.tables]


def pack_and_delete(repo: Repos, r, cutoff, pack: PrunePack, tz: str) -> int:
    """一批一批：讀 → 打包 → 刪。打包寫不進去丟例外，那一批不刪。"""
    n = 0
    while True:
        rows = r.prune_rows(cutoff, pack.batch)
        if not rows:
            return n
        repo.packed.add(r.TABLE, rows, tz)
        gone = r.delete_ids([x["id"] for x in rows])
        n += gone
        if len(rows) < pack.batch or gone == 0:
            return n


def cmd_prune(prune_config: PruneConfig, args, repo: Repos | None = None) -> int:
    repo = repo or Repos(None, load_scenario())       # 只動 PostgreSQL，不用 ES（ES_URL 沒設也能跑）
    now = now_of(args.at)
    dry = bool(getattr(args, "dry_run", False))
    tz = prune_config.timezone
    pack = prune_config.pack
    how = "打包" if pack else "刪"
    total = 0
    for p in plan(prune_config, now):
        r = getattr(repo, p["table"])
        when = f"早於 {p['cutoff']:%Y-%m-%d %H:%M}，保留 {p['keep_days']:g} 天"
        if dry:
            n = r.prune_count(p["cutoff"])
            print(f"  {p['table']}：{when}的有 {n} 筆" + ("，沒動（--dry-run）" if n else ""))
        elif pack:
            try:
                n = pack_and_delete(repo, r, p["cutoff"], pack, tz)
            except Exception as e:                     # 資料庫寫不進去（磁碟滿、權限）：這一批沒刪，下一次再試
                print(f"  {p['table']}：打包寫不進去（{e}）；這一批沒刪，下一次再試")
                return 1
            print(f"  {p['table']}：打包並刪了 {n} 筆（{when}）")
        else:
            n = r.prune(p["cutoff"])
            print(f"  {p['table']}：刪了 {n} 筆（{when}）")
        total += n
    if pack and pack.keep_days is not None and not dry:
        cutoff = (now - pd.Timedelta(days=float(pack.keep_days))).to_pydatetime()
        print(f"  打包的資料：超過 {pack.keep_days:g} 天的刪了 {repo.packed.prune(cutoff)} 包")
    print(f"{now.tz_convert(tz):%m-%d %H:%M}：共 {total} 筆" + (f"要{how}" if dry else ""))
    return 0

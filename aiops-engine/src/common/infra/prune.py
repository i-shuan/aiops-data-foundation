"""prune：engine 自己的資料（PostgreSQL）超過保留天數的每天清一次（configs/baseline/prune.yaml）。

清哪些、依什麼時間由各 repository 的 PRUNE_WHERE 決定：異常、trace 每段的統計依自己的時間；告警只刪已結案的、依結案時間
（開著的件、狀態文件不會被刪）。人工判斷的事故、回饋、調整不在這裡：事故紀錄要長留，掉了不能重算（備份交給 DBA 的 PostgreSQL 備份）。

prune.yaml 有 archive（或設了 AIOPS_ARCHIVE_DIR）時，刪之前先把那幾筆壓縮存成檔案（common/infra/archive.py），一批一批：
讀 batch 筆 → 寫檔、fsync → 才刪那幾筆。歸檔資料夾寫不進去就一筆都不刪、回 1（排程會標失敗，下一次再試）。
"""
from __future__ import annotations

import pandas as pd

import common.infra.archive as ARC
from common.clock import now_of
from common.config.settings import settings
from common.infra.repos import Repos
from common.scenario import load_scenario
from common.config.prune import PruneArchive, PruneConfig


def plan(prune_config: PruneConfig, now: pd.Timestamp) -> list[dict]:
    """每種資料要刪多早以前的：{"table", "keep_days", "cutoff"}。"""
    return [{"table": t.table, "keep_days": float(t.keep_days), "cutoff": (now - pd.Timedelta(days=float(t.keep_days))).to_pydatetime()}
            for t in prune_config.tables]


def archive_of(prune_config: PruneConfig) -> PruneArchive | None:
    """要不要歸檔、歸到哪：環境變數 AIOPS_ARCHIVE_DIR（off = 不歸檔）蓋過 prune.yaml 的 archive.dir。"""
    env = (settings().aiops_archive_dir or "").strip()
    if env.lower() == "off":
        return None
    if env:
        return (prune_config.archive or PruneArchive()).model_copy(update={"dir": env})
    return prune_config.archive


def archive_and_delete(r, cutoff, arc: PruneArchive, tz: str) -> int:
    """一批一批：讀 → 寫檔 → 刪。寫檔失敗丟 OSError，那一批不刪。"""
    n = 0
    while True:
        rows = r.prune_rows(cutoff, arc.batch)
        if not rows:
            return n
        ARC.write(arc.dir, r.TABLE, rows, tz)
        gone = r.delete_ids([x["id"] for x in rows])
        n += gone
        if len(rows) < arc.batch or gone == 0:
            return n


def cmd_prune(prune_config: PruneConfig, args, repo: Repos | None = None) -> int:
    repo = repo or Repos(None, load_scenario())       # 只動 PostgreSQL，不用 ES（ES_URL 沒設也能跑）
    now = now_of(args.at)
    dry = bool(getattr(args, "dry_run", False))
    tz = prune_config.timezone
    arc = archive_of(prune_config)
    if arc and not dry:
        try:
            ARC.check_writable(arc.dir)
        except OSError as e:
            print(f"歸檔資料夾 {arc.dir} 寫不進去（{e}）：一筆都沒刪。掛好資料夾，或 AIOPS_ARCHIVE_DIR=off 不歸檔直接刪")
            return 1
    where = f"，歸檔到 {arc.dir}" if arc else ""
    total = 0
    for p in plan(prune_config, now):
        r = getattr(repo, p["table"])
        when = f"早於 {p['cutoff']:%Y-%m-%d %H:%M}，保留 {p['keep_days']:g} 天"
        if dry:
            n = r.prune_count(p["cutoff"])
            print(f"  {p['table']}：{when}的有 {n} 筆" + (f"，沒刪（--dry-run{where}）" if n else ""))
        elif arc:
            try:
                n = archive_and_delete(r, p["cutoff"], arc, tz)
            except OSError as e:
                print(f"  {p['table']}：歸檔寫不進去（{e}）；這一批沒刪，下一次再試")
                return 1
            print(f"  {p['table']}：歸檔並刪了 {n} 筆（{when}）→ {arc.dir}/{r.TABLE}/")
        else:
            n = r.prune(p["cutoff"])
            print(f"  {p['table']}：刪了 {n} 筆（{when}）")
        total += n
    if arc and arc.keep_days is not None and not dry:
        gone = ARC.expire(arc.dir, arc.keep_days, now.tz_convert(tz).date())
        print(f"  歸檔檔案：超過 {arc.keep_days:g} 天的刪了 {len(gone)} 個")
    print(f"{now.tz_convert(tz):%m-%d %H:%M}：共 {total} 筆" + ("要刪" if dry else "") + where)
    return 0

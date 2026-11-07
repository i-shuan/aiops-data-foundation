"""detector db：engine 自己的資料（PostgreSQL）。

  python3 -m main detector db init                    建表（每個工作第一次寫入也會自己建；給 DBA 先建好、看權限夠不夠用）
  python3 -m main detector db status                  每張表幾筆、pgvector 有沒有裝
  python3 -m main detector db import-es [--only alerts,incidents] [--dry-run]   以前存在 ES 的 aiops-* 搬過來（一次性）
  python3 -m main detector db restore [--table aiops_anomalies] [--from 2026-07-01] [--to 2026-07-31] [--dry-run]   prune 打包的資料讀回原表
"""
from __future__ import annotations

import typer

import common.infra.es_import as IMP
import common.infra.pack as PACK
from common.infra.es import ES
from common.infra.repos import Repos
from common.scenario import load_scenario
from common.scenario.mapping import MappingRepository
from interface.cli.common import Opt

db = typer.Typer(add_completion=False, rich_markup_mode=None, help="engine 自己的資料（PostgreSQL）：init、status、import-es、restore")
COMMANDS = {"db": lambda app: app.add_typer(db, name="db")}
TABLES = ("packed", "anomalies", "baseline", "alerts", "incidents", "incident_vectors", "feedback", "tuning", "state", "chronic", "segments")


def _targets(repo: Repos) -> dict:
    out = {n: getattr(repo, n) for n in TABLES}
    out["mapping"] = MappingRepository(db=repo.db)
    return out


@db.command("init", help="建全部的表與索引（已經有的不動）")
def init_cmd() -> int:
    repo = Repos(None, load_scenario())
    for name, r in _targets(repo).items():
        r.ensure()
        print(f"  {r.TABLE}")
    print(f"建好了；pgvector {'有裝（向量用 SQL 算）' if repo.db.has_vector() else '沒裝（向量在程式裡算，事故幾百件以內沒差）'}")
    return 0


@db.command("status", help="每張表幾筆、pgvector 有沒有裝")
def status_cmd() -> int:
    repo = Repos(None, load_scenario())
    for name, r in _targets(repo).items():
        print(f"  {r.TABLE:28} {r.count():>10,}")
    print(f"pgvector：{'有' if repo.db.has_vector() else '沒有'}")
    return 0


@db.command("import-es", help="以前存在 ES 的 aiops-* 搬進 PostgreSQL（一次性；同 id 覆蓋，可以重跑）")
def import_cmd(only: str = Opt("", help="只搬這幾種，逗號隔開：anomalies、alerts、incidents、feedback、tuning、mapping…"),
               dry_run: bool = Opt(False, "--dry-run", help="只數 ES 裡有幾筆，不寫")) -> int:
    repo = Repos(ES(), load_scenario())
    names = {x.strip() for x in only.split(",") if x.strip()} or None
    bad = (names or set()) - set(IMP.LEGACY.values())
    if bad:
        print(f"不認得：{'、'.join(sorted(bad))}（可以是 {'、'.join(IMP.LEGACY.values())}）")
        return 2
    for index, table, n in IMP.run(repo.es, _targets(repo), names, dry_run):
        print(f"  {index:24} → {table:24} {n:>10,} 筆" + ("（--dry-run，沒寫）" if dry_run else ""))
    print("ES 那邊沒刪：對過筆數（detector db status）、Grafana 改讀 PostgreSQL 之後再手動刪")
    return 0


@db.command("restore", help="prune 打包的資料（aiops_packed）解開、寫回原本的表（照原本的 id；同 id 覆蓋，可以重跑）")
def restore_cmd(table: str = Opt("", help="只讀回這張表，例如 aiops_anomalies；不寫 = 全部"),
                start: str = Opt("", "--from", help="從這一天（含），YYYY-MM-DD"),
                end: str = Opt("", "--to", help="到這一天（含），YYYY-MM-DD"),
                dry_run: bool = Opt(False, "--dry-run", help="只數有幾筆，不寫")) -> int:
    repo = Repos(None, load_scenario())
    by_table = {r.TABLE: r for r in _targets(repo).values()}
    if table and table not in by_table:
        print(f"不認得 {table}（可以是 {'、'.join(by_table)}）")
        return 2
    counts: dict[str, int] = {}
    for pk in repo.packed.packs(table or None, start or None, end or None):
        t = pk["table"]
        if t not in by_table:
            continue
        rows = PACK.unpack(pk["data"])
        if not dry_run:
            by_table[t].restore(rows)
        counts[t] = counts.get(t, 0) + len(rows)
    for t, n in sorted(counts.items()):
        print(f"  {t:28} {n:>10,} 筆" + ("（--dry-run，沒寫）" if dry_run else ""))
    if not counts:
        print("沒有符合的打包資料（aiops_packed 是空的，或日期範圍外）")
    elif not dry_run:
        print("注意：讀回來的還是比保留天數舊，下一次 prune（每天 03:30）會再打包、刪掉；查完就好，或先停掉 prune")
    return 0

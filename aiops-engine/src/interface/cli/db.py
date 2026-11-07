"""detector db：engine 自己的資料（PostgreSQL）。

  python3 -m main detector db init                    建表（每個工作第一次寫入也會自己建；給 DBA 先建好、看權限夠不夠用）
  python3 -m main detector db status                  每張表幾筆、pgvector 有沒有裝
  python3 -m main detector db import-es [--only alerts,incidents] [--dry-run]   以前存在 ES 的 aiops-* 搬過來（一次性）
  python3 -m main detector db restore <檔案或資料夾…> [--table aiops_anomalies] [--dry-run]   prune 歸檔的 .jsonl.gz 讀回來
"""
from __future__ import annotations

import os

import typer

import common.infra.archive as ARC
import common.infra.es_import as IMP
from common.infra.es import ES
from common.infra.repos import Repos
from common.scenario import load_scenario
from common.scenario.mapping import MappingRepository
from interface.cli.common import Opt

db = typer.Typer(add_completion=False, rich_markup_mode=None, help="engine 自己的資料（PostgreSQL）：init、status、import-es、restore")
COMMANDS = {"db": lambda app: app.add_typer(db, name="db")}
TABLES = ("anomalies", "baseline", "alerts", "incidents", "incident_vectors", "feedback", "tuning", "state", "chronic", "segments")


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


@db.command("restore", help="prune 歸檔的 .jsonl.gz 讀回 PostgreSQL（照原本的 id；同 id 覆蓋，可以重跑）")
def restore_cmd(paths: list[str] = typer.Argument(..., help="歸檔的檔案或資料夾（資料夾找底下全部的 .jsonl.gz）"),
                table: str = Opt("", help="只讀回這張表，例如 aiops_anomalies"),
                dry_run: bool = Opt(False, "--dry-run", help="只數有幾筆，不寫")) -> int:
    repo = Repos(None, load_scenario())
    by_table = {r.TABLE: r for r in _targets(repo).values()}
    if table and table not in by_table:
        print(f"不認得 {table}（可以是 {'、'.join(by_table)}）")
        return 2
    missing = [x for x in paths if not os.access(x, os.R_OK)]
    if missing:
        print(f"讀不到：{'、'.join(missing)}（路徑對嗎？有沒有權限？）")
        return 2
    counts: dict[str, int] = {}
    pending: dict[str, list] = {}

    def flush(t: str) -> None:
        rows = pending.pop(t, [])
        if rows and not dry_run:
            by_table[t].restore(rows)
        counts[t] = counts.get(t, 0) + len(rows)

    for f in ARC.files(paths):
        for row in ARC.read(f):
            t = row.get("table")
            if t not in by_table or (table and t != table):
                continue
            pending.setdefault(t, []).append((row["id"], row.get("ts"), row["doc"]))
            if len(pending[t]) >= 5000:        # 一天的 trace 統計可能幾十萬筆：分批寫，記憶體不會跟著檔案變大
                flush(t)
    for t in list(pending):
        flush(t)
    for t, n in sorted(counts.items()):
        print(f"  {t:28} {n:>10,} 筆" + ("（--dry-run，沒寫）" if dry_run else ""))
    if not counts:
        print("沒有讀到資料（路徑對嗎？檔名是 <表>-<年-月-日>.jsonl.gz）")
    elif not dry_run:
        print("注意：讀回來的還是比保留天數舊，下一次 prune（每天 03:30）會再歸檔、刪掉；查完就好，或先停掉 prune")
    return 0

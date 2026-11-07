"""detector mapping：欄位對照的覆寫（歸屬、log 的欄位名、trace 的 raw 索引）。提、試、核准、退回、停用（data/mapping_service.py）。

  python3 -m main detector mapping list
  python3 -m main detector mapping propose --source apm --section owner --field service --value labels.app --by 王小明 --reason 我們的服務名在 label
  python3 -m main detector mapping propose --source apm --section trace --field raw_index --value 'traces-apm.*-*' --by 王小明
  python3 -m main detector mapping try --source apm --section owner --field service --value labels.app      （只試、不存）
  python3 -m main detector mapping approve --id <id> --by 李主管 [--force]
  python3 -m main detector mapping reject --id <id> --by 李主管
  python3 -m main detector mapping disable --id <id> --by 李主管                 （退回 yaml 的值）
核准只在命令列（有權限進 pod、跑排程的人）；網頁（/mapping）只能看與提。
"""
from __future__ import annotations

from typing import Callable

import typer

import data.mapping_service as M
from interface.config.schedule import trace_raw_pairs
from common.infra.es import ES
from common.scenario import load_scenario
from interface.cli.common import Opt, group

COMMANDS: dict[str, Callable[[typer.Typer], None]] = {}
mapping = group("mapping", help="欄位對照的覆寫（歸屬、log 的欄位名、trace 的 raw 索引）：list、propose、try、approve、reject、disable；核准過的蓋在情境資料檔上，下一輪工作生效")
COMMANDS["mapping"] = lambda app: app.add_typer(mapping)


def _show(findings) -> None:
    if not findings:
        print("  試過：欄位在抽到的資料裡有出現")
    for f in findings:
        print(f"  {'⚠' if f.counts else 'ℹ'} {f}")


@mapping.command("list", help="每個來源、每個開放的欄位：yaml 的值、現在生效的值；下面列出所有提過的覆寫")
def list_cmd() -> int:
    es, sc = ES(), load_scenario()
    for r in M.current(es, sc, trace_raw_pairs(sc.schedule)):
        mark = f"  ← 覆寫 {r['override']}" if r["override"] else ""
        print(f"{r['source']:5} {r['section']:5} {r['field']:15} {r['value']!s:45}{'' if r['value'] == r['yaml'] else '（yaml：' + str(r['yaml']) + '）'}{mark}")
    print("\n提過的覆寫（新的在前）：")
    for d in M.MappingRepository(es).all():
        print(f"  {d['id']}  [{d.get('status')}] {d.get('source')}.{d.get('section')}.{d.get('field')}：{d.get('old_value')} → {d.get('value')}"
              f"（{d.get('by')}，{str(d.get('created_at', ''))[:16]}）{' ' + d['reason'] if d.get('reason') else ''}")
    return 0


@mapping.command("try", help="只試、不存：新的值在真的資料裡有沒有出現")
def try_cmd(source: str = Opt(...), section: str = Opt(...), field: str = Opt(...), value: str = Opt(..., help="清單欄位用逗號隔開")) -> int:
    try:
        _show(M.try_value(ES(), load_scenario(), source, section, field, value))
    except ValueError as e:
        print(e)
        return 2
    return 0


@mapping.command("propose", help="提一筆（status proposed，還沒生效），順便試一次")
def propose_cmd(source: str = Opt(...), section: str = Opt(...), field: str = Opt(...), value: str = Opt(..., help="清單欄位用逗號隔開"),
                by: str = Opt(...), reason: str = Opt("")) -> int:
    try:
        sc = load_scenario()
        _id, findings = M.propose(ES(), sc, source, section, field, value, by, reason, trace_raw_pairs(sc.schedule))
    except ValueError as e:
        print(e)
        return 2
    print(f"已提：{_id}（proposed，還沒生效；核准：detector mapping approve --id {_id} --by <你>）")
    _show(findings)
    return 0


@mapping.command("approve", help="proposed → active（核准前再試一次；對不上不給，--force 才行）")
def approve_cmd(id_: str = Opt(..., "--id"), by: str = Opt(...), force: bool = Opt(False, "--force", help="試不過也硬核准（例如資料還沒進來）")) -> int:
    ok, msg = M.approve(ES(), load_scenario(), id_, by, force)
    print(msg)
    return 0 if ok else 2


@mapping.command("reject", help="proposed → rejected")
def reject_cmd(id_: str = Opt(..., "--id"), by: str = Opt(...)) -> int:
    ok, msg = M.set_status(ES(), id_, "rejected", by)
    print(msg)
    return 0 if ok else 2


@mapping.command("disable", help="active → disabled（退回 yaml 的值）")
def disable_cmd(id_: str = Opt(..., "--id"), by: str = Opt(...)) -> int:
    ok, msg = M.set_status(ES(), id_, "disabled", by)
    print(msg)
    return 0 if ok else 2

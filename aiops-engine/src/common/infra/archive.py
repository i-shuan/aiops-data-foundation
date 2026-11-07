"""過期資料的歸檔：detector prune 刪之前先把那幾筆壓縮存成檔案；detector db restore 讀回（docs/pdca/meta/2026-11-prune-archive.md）。

    <歸檔資料夾>/<表>/<年>/<表>-<年-月-日>.jsonl.gz      一天一個檔（依那筆的時間、prune.yaml 的時區）
    一行一筆：{"table": 表, "id": …, "ts": …, "doc": {整份文件}}

同一天的檔再歸檔時接在後面（gzip 允許好幾段接在一起，gzip -dc、zcat 照樣讀）。寫好、fsync 之後才刪資料庫那幾筆，
所以寫到一半當掉時，最後一段可能不完整，但那幾筆還沒刪、下一次會再歸檔一次；讀的時候不完整的尾巴跳過，同 id 寫回是覆蓋，不會重複。
"""
from __future__ import annotations

import gzip
import json
import os
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SUFFIX = ".jsonl.gz"


def _day(ts, tz: ZoneInfo) -> str:
    return ts.astimezone(tz).date().isoformat() if isinstance(ts, datetime) else "undated"


def path_of(root: str | Path, table: str, day: str) -> Path:
    return Path(root) / table / (day[:4] if day[:4].isdigit() else day) / f"{table}-{day}{SUFFIX}"


def check_writable(root: str | Path) -> None:
    """歸檔資料夾建得起來、寫得進去；不行丟 OSError（prune 就一筆都不刪）。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    probe = root / ".write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def write(root: str | Path, table: str, rows: list[dict], tz: str = "UTC") -> list[Path]:
    """rows：[{id, ts, doc}]（PgRepository.prune_rows）。依天接到各自的檔，fsync 完才回；寫不進去丟 OSError。"""
    zone, by_day = ZoneInfo(tz), defaultdict(list)
    for r in rows:
        ts = r.get("ts")
        by_day[_day(ts, zone)].append(json.dumps({"table": table, "id": r["id"], "ts": ts.isoformat() if isinstance(ts, datetime) else None,
                                                  "doc": r["doc"]}, ensure_ascii=False, default=str))
    out = []
    for day, lines in sorted(by_day.items()):
        p = path_of(root, table, day)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
                gz.write(("\n".join(lines) + "\n").encode("utf-8"))
            raw.flush()
            os.fsync(raw.fileno())
        out.append(p)
    return out


def files(paths: Iterable[str | Path]) -> list[Path]:
    """檔案照給的；資料夾找底下全部的 .jsonl.gz（依名字排，等於依表、依天）。"""
    out = []
    for p in map(Path, paths):
        out += sorted(x for x in p.rglob(f"*{SUFFIX}")) if p.is_dir() else [p]
    return out


def read(path: str | Path) -> Iterator[dict]:
    """一行一筆；最後一段不完整（寫到一半當掉）就停在那裡（那幾筆當時沒刪，別的檔或下一次歸檔有）。"""
    try:
        with gzip.open(path, "rb") as f:
            for line in f:
                if line.endswith(b"\n"):          # 沒有換行的是被截斷的最後一行
                    yield json.loads(line)
    except (EOFError, gzip.BadGzipFile):
        return


def expire(root: str | Path, keep_days: float, today: date) -> list[Path]:
    """歸檔檔案本身只留 keep_days 天：檔名的日期早於 today - keep_days 的刪掉。回刪了哪些。"""
    cutoff = (today - timedelta(days=float(keep_days))).isoformat()
    gone = []
    for p in files([root]):
        d = "-".join(p.name[: -len(SUFFIX)].rsplit("-", 3)[-3:])      # <表>-年-月-日
        if len(d) == 10 and d[:4].isdigit() and d < cutoff:
            p.unlink()
            gone.append(p)
    return gone

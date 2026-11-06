"""歷史事故單匯入（docs/pdca/phase4-rca-llm/2026-10-incident-import.md）。

  python3 -m investigator import-incidents <csv 或 json> [--tz Asia/Taipei] [--dry-run]
  python3 -m investigator import-incidents --db 'postgresql://user:${PW}@host/db' --sql "SELECT subject AS title, … FROM incidents"
  （或 rca.yaml 寫 incidents_db: {url, sql}，排程定期匯：docs/pdca/meta/2026-11-case-classify.md）

一列一件，欄位就是 aiops-incidents 的（title、start、end、services、root_cause_category、root_cause、action、how_detected、checked；
可選 by、note；有的話再給指紋欄位 routes、series、slow_segments、error_origins、patterns）。
寫進去的是 status confirmed、source import：相似事故檢索（similar.py）馬上拿得到，similar-eval 也算得到。

- 缺必要欄位、時間看不懂的那一筆略過，印出第幾筆缺什麼；其他照匯。
- 時間沒寫時區當台北（detector.adjust.local_iso，跟行事曆、incident 指令一樣）。
- _id 是內容指紋（title＋start＋services）：同一份檔重跑覆蓋、不重複；改了 action 再匯也是覆蓋同一筆。
- alert_id 是 imp-<事故日期>-<檔案裡第幾筆>，只是給人看、對得回檔案。
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiops.core.connection import ES, doc_id
from aiops.core.entity.incident import INCIDENT_INDEX, Fingerprint, Incident
from aiops.core.repository import Repos
from aiops.scenario import load_scenario
from aiops.service.detect.adjustments import local_iso

REQUIRED = ("title", "start", "end", "services", "root_cause_category", "root_cause", "action", "how_detected", "checked")
OPTIONAL = ("by", "note")
# 指紋欄位（有更好：慢的那段、錯的源頭、API、指標、log 樣式）。沒有就只用 services 比對
FP_FIELDS = ("routes", "series", "slow_segments", "error_origins", "patterns")
LIST_FIELDS = ("services", "checked") + FP_FIELDS
DEFAULT_TZ = "Asia/Taipei"


class DbRows:
    """從資料庫讀：SQL 一句，欄位別名就是 CSV 的欄位名（SELECT subject AS title, opened AS start, … FROM incidents）。
    url 是 SQLAlchemy 的；sqlite:/// 不用裝 SQLAlchemy（標準庫）。url 裡的 ${VAR} 用環境變數換掉（密碼不寫進設定檔）。"""

    def __init__(self, url: str, sql: str):
        self.url, self.sql = os.path.expandvars(url), sql

    def __str__(self):
        return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", self.url)   # 印出來時遮密碼

    def rows(self) -> list[dict]:
        if self.url.startswith("sqlite:///"):
            con = sqlite3.connect(self.url[len("sqlite:///"):])
            con.row_factory = sqlite3.Row
            try:
                return [dict(r) for r in con.execute(self.sql)]
            finally:
                con.close()
        try:
            import sqlalchemy
        except ImportError:
            raise SystemExit(f"讀 {self} 要裝 SQLAlchemy（pip install sqlalchemy 和對應的驅動）；sqlite 不用")
        with sqlalchemy.create_engine(self.url).connect() as con:
            res = con.execute(sqlalchemy.text(self.sql))
            return [dict(r._mapping) for r in res]


def read_rows(path: "str | Path | DbRows") -> list[dict]:
    """CSV（Excel 存的 UTF-8 BOM 也行）或 JSON 陣列；JSON 也接受 {"incidents": [...]}（跟 owners、calendar 一樣可包可不包）；
    或 DbRows（資料庫）。"""
    if isinstance(path, DbRows):
        return path.rows()
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and "incidents" in data:
            data = data["incidents"]
        if not isinstance(data, list):
            raise SystemExit(f"{p}：JSON 要是陣列（一件一個物件），或 {{\"incidents\": [...]}}")
        return [r for r in data if isinstance(r, dict)]
    with open(p, encoding="utf-8-sig", newline="") as f:
        return [{(k or "").strip(): v for k, v in r.items()} for r in csv.DictReader(f)]


def split_list(v) -> list[str]:
    """多值欄位：JSON 給陣列；CSV 用逗號分隔，值本身有逗號（SQL）就改用 |。"""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        items = [str(x) for x in v]
    else:
        s = str(v)
        items = s.split("|") if "|" in s else s.split(",")
    return [x.strip() for x in items if x and x.strip()]


def _text(v) -> str | None:
    s = ("" if v is None else str(v)).strip()
    return s or None


def _time(v, tz: str) -> str | None:
    """沒寫時區的當 tz 的當地時間。看不懂回 None（呼叫端當成缺欄位）。"""
    s = _text(v)
    if not s:
        return None
    try:
        return local_iso(s, tz)
    except (ValueError, TypeError):
        return None


def normalize(row: dict, n: int, tz: str = DEFAULT_TZ, now: datetime | None = None) -> tuple[tuple[str, dict] | None, str | None]:
    """一列 → (_id, 文件)；不能匯的回 (None, 原因)。n 是檔案裡第幾筆（從 1 起），進 alert_id 和訊息。"""
    missing = [k for k in REQUIRED if (not split_list(row.get(k)) if k in LIST_FIELDS else not _text(row.get(k)))]
    if missing:
        return None, f"缺 {'、'.join(missing)}"
    start, end = _time(row["start"], tz), _time(row["end"], tz)
    if not start or not end:
        bad = [f"{k}「{row[k]}」" for k, t in (("start", start), ("end", end)) if not t]
        return None, f"時間看不懂：{'、'.join(bad)}"
    if datetime.fromisoformat(end) < datetime.fromisoformat(start):
        return None, f"end（{end}）早於 start（{start}）"
    now = now or datetime.now(timezone.utc)
    services = sorted(set(split_list(row["services"])))
    day = datetime.fromisoformat(start).astimezone(ZoneInfo(tz)).strftime("%Y%m%d")    # 事故日期用當地日期
    by = _text(row.get("by"))
    inc = Incident(**{"@timestamp": now.isoformat()}, title=_text(row["title"]), start=start, end=end, services=services,
                   status="confirmed", source="import", detected_at=start, resolved_at=end,
                   root_cause_category=_text(row["root_cause_category"]), root_cause=_text(row["root_cause"]),
                   action=_text(row["action"]), how_detected=_text(row["how_detected"]), checked=split_list(row["checked"]),
                   fingerprint=fingerprint(row, services), by=by, confirmed_by=by, note=_text(row.get("note")),
                   alert_id=f"imp-{day}-{n:02d}")
    return (doc_id("import", inc.title, start, services), inc.to_doc()), None


def fingerprint(row: dict, services: list[str]) -> Fingerprint:
    """匯入檔一列的指紋欄位 → Fingerprint；正規化（去數字、截幾個）跟自動產生的同一處（Fingerprint.normalized），才對得起來。"""
    return Fingerprint.normalized(services=services, routes=split_list(row.get("routes")), series=split_list(row.get("series")),
                                  slow_segments=split_list(row.get("slow_segments")), error_origins=split_list(row.get("error_origins")),
                                  patterns=split_list(row.get("patterns")))


def parse(path: "str | Path | DbRows", tz: str = DEFAULT_TZ, now: datetime | None = None) -> tuple[list[tuple[str, dict]], list[tuple[int, str]]]:
    """整個檔 → (能匯的 [(_id, doc)], 略過的 [(第幾筆, 原因)])。"""
    docs, skipped = [], []
    for n, row in enumerate(read_rows(path), 1):
        item, why = normalize(row, n, tz, now)
        if item:
            docs.append(item)
        else:
            skipped.append((n, why))
    return docs, skipped


def write(repo, docs: list[tuple[str, dict]]) -> int:
    """index（不是 update）：匯入的檔案是這幾件的真相，重匯就覆蓋，包括回饋網頁後來改過的欄位。"""
    if not docs:
        return 0
    repo.incidents.ensure()
    repo.incidents.bulk(docs)
    return len(docs)


def run(path: "str | DbRows", tz: str = DEFAULT_TZ, dry_run: bool = False, repo=None) -> int:
    if isinstance(path, DbRows):
        print(f"從資料庫讀：{path}")
    docs, skipped = parse(path, tz)
    for n, why in skipped:
        print(f"  第 {n} 筆略過：{why}")
    dup = len(docs) - len({i for i, _ in docs})
    if dup:
        print(f"  注意：{dup} 筆內容相同（title、start、services 一樣），只會留一筆")
    total = len(docs) + len(skipped)
    if dry_run:
        print(f"檢查完：{len(docs)} 筆可匯入、{len(skipped)} 筆略過（共 {total} 筆；--dry-run 沒有寫入）")
        return 0 if docs or not total else 1
    n = write(repo or Repos(ES(), load_scenario()), docs)
    print(f"匯入 {n} 筆、略過 {len(skipped)} 筆（共 {total} 筆）→ {INCIDENT_INDEX}（status confirmed、source import）")
    return 0

"""本機日誌：每一筆寫進 PostgreSQL 的動作，先記在本機（PVC）一份，再寫資料庫（docs/pdca/meta/2026-11-pg-resilience.md）。

    <AIOPS_JOURNAL_DIR>/<資料庫代號>-<主機>-<pid>-<開始時間>.jsonl     一個程式一個檔，一行一個寫入動作（SQL＋參數）

寫入的順序：記進檔案、fsync → 照順序寫資料庫（前面還沒寫進去的先寫）→ 全部寫進去了就把檔案清空。所以檔案裡永遠只有
「還沒確定寫進資料庫的」。資料庫寫不進去：這一筆留在檔案、丟出錯誤（工作照樣失敗，跟沒有日誌時一樣），
同一個程式下一次寫入會先補它；程式結束了還沒補完，檔案留著，之後別的程式（排程器每分鐘、下一個工作開始時）認領、照順序補寫。
認領是把檔案改名（只有一個人改得成功），補寫失敗再改回來。

只記 INSERT／UPDATE／DELETE（建表、查詢不記）。補寫可能重做「已經寫進去、只是還沒來得及清空檔案」的那一筆：
engine 的寫入都是同 id 覆蓋、合併欄位、依條件刪，重做結果一樣。
AIOPS_JOURNAL_DIR 沒設：不記，直接寫資料庫（跟以前一樣）。
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

WRITES = ("INSERT", "UPDATE", "DELETE")
CLAIM = ".claimed-"
STALE_SECONDS = 120          # 別台主機的檔案超過這麼久沒動才認領（那台可能還活著、自己會補）


class JournalUnwritable(Exception):
    """本機寫不進去（沒權限、磁碟滿）：呼叫端照樣寫資料庫（本機那一份是額外的保險，不能變成新的故障點）。"""


def is_write(sql: str) -> bool:
    head = sql.lstrip().split(None, 1)
    return bool(head) and head[0].upper() in WRITES


def _enc(v):
    if isinstance(v, datetime):
        return {"$dt": v.isoformat()}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if hasattr(v, "isoformat"):                       # pandas Timestamp、date
        return {"$dt": v.isoformat()}
    if hasattr(v, "item"):                            # numpy 數字
        return v.item()
    return str(v)


def _dec(v):
    if isinstance(v, dict):
        if "$dt" in v:
            return datetime.fromisoformat(v["$dt"])
        if "$b" in v:
            return base64.b64decode(v["$b"])
    if isinstance(v, list):
        return [_dec(x) for x in v]
    return v


def _params(p, many: bool):
    if p is None:
        return None
    return [tuple(_dec(r)) for r in p] if many else tuple(_dec(p))


def db_key(dsn: str, schema: str | None) -> str:
    """同一個資料庫（同 DSN、同 schema）的檔案才互相補寫。"""
    return hashlib.sha1(f"{dsn}|{schema or ''}".encode()).hexdigest()[:10]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Journal:
    def __init__(self, root: str, key: str):
        self.root, self.key = Path(root), key
        self.root.mkdir(parents=True, exist_ok=True)
        self.host = socket.gethostname()
        self.path = self.root / f"{key}-{self.host}-{os.getpid()}-{int(time.time() * 1000)}.jsonl"
        self.pending: list[dict] = []
        self.lock = threading.Lock()
        self.f = None
        self.recovered = False
        atexit.register(self.close)

    # ── 這個程式自己的 ──
    def _append(self, entry: dict) -> None:
        if self.f is None:
            self.f = open(self.path, "a", encoding="utf-8")
        self.f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.f.flush()
        os.fsync(self.f.fileno())

    def _clear(self) -> None:
        if self.f is not None:
            self.f.truncate(0)
            self.f.seek(0)
            self.f.flush()
            os.fsync(self.f.fileno())

    def write(self, apply, sql: str, params, many: bool):
        """記下來、再照順序寫（apply(sql, params, many) 真的送資料庫）。回最後這一筆的結果（rowcount）。"""
        with self.lock:
            try:
                self._append({"at": datetime.now().astimezone().isoformat(), "sql": sql,
                              "params": _enc(list(params) if many else params) if params is not None else None, "many": many})
            except OSError as e:
                if self.pending:                 # 前面還有沒寫進去的：先試著補（照順序），補不了就照常丟資料庫的錯
                    while self.pending:
                        e0 = self.pending[0]
                        apply(e0["sql"], e0["params"], e0["many"])
                        self.pending.pop(0)
                raise JournalUnwritable(str(e)) from e
            self.pending.append({"sql": sql, "params": params, "many": many})
            out = None
            while self.pending:
                e = self.pending[0]
                out = apply(e["sql"], e["params"], e["many"])          # 寫不進去就丟出去：留在 pending 與檔案
                self.pending.pop(0)
            self._clear()
            return out

    def close(self) -> None:
        if self.f is None:
            return
        try:
            self.f.close()
            if not self.pending and self.path.exists() and self.path.stat().st_size == 0:
                self.path.unlink()
        except OSError:
            pass
        self.f = None

    # ── 別的程式留下的 ──
    def leftovers(self) -> list[Path]:
        """可以認領的：同一個資料庫、不是自己、(同主機而且那個程式不在了) 或 (別台主機而且很久沒動)。依開始時間排。"""
        out, now = [], time.time()
        for p in self.root.glob(f"{self.key}-*.jsonl"):
            if p == self.path or CLAIM in p.name:
                continue
            try:
                _, host, pid, _ms = p.stem.rsplit("-", 3)
                st = p.stat()
            except (ValueError, OSError):
                continue
            if st.st_size == 0 and (host != self.host or not _alive(int(pid))):
                p.unlink(missing_ok=True)
                continue
            mine = host == self.host
            if (mine and not _alive(int(pid))) or (not mine and now - st.st_mtime > STALE_SECONDS):
                out.append(p)
        return sorted(out, key=lambda p: int(p.stem.rsplit("-", 1)[1]))

    def recover(self, apply) -> int:
        """把別的程式留下、還沒寫進資料庫的照順序補寫。回補了幾筆；補到一半寫不進去就停（檔案放回去，下次再補）。"""
        n = 0
        for p in self.leftovers():
            claimed = p.with_name(p.name + CLAIM + str(os.getpid()))
            try:
                os.rename(p, claimed)                                    # 只有一個人改得成功
            except OSError:
                continue
            lines = [ln for ln in claimed.read_text(encoding="utf-8").splitlines() if ln.strip()]
            done = 0
            try:
                for ln in lines:
                    try:
                        e = json.loads(ln)
                    except json.JSONDecodeError:                         # 寫到一半當掉的最後一行（那一筆當時也沒寫進資料庫）
                        done += 1
                        continue
                    apply(e["sql"], _params(e["params"], e["many"]), e["many"])
                    done += 1
                    n += 1
            except Exception:
                rest = lines[done:]
                claimed.write_text("".join(x + "\n" for x in rest), encoding="utf-8")
                os.rename(claimed, p)                                    # 放回去
                raise
            claimed.unlink(missing_ok=True)
        return n

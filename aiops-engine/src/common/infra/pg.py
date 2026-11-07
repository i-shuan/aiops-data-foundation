"""PostgreSQL：engine 自己的資料（異常、基準線、告警、事故、回饋、調整、對照覆寫…）存這裡（docs/pdca/meta/2026-11-postgres-store.md）。
原始資料（Elastic APM 寫的 log、metrics、trace）與 traces-merged-* 還是在 ES，用 common/infra/es.py。

一張表一種文件，欄位都一樣：
  id    文件 id（同樣的內容算出同一個 id，doc_id；同 id 再寫一次覆蓋，重跑不重複）
  ts    這張表的時間欄（PgRepository.TIME 指定文件裡哪個欄位，例如 @timestamp、started_at），依時間查、清舊資料用
  doc   整份文件（jsonb）
常查的欄位在各 repository 的 INDEXES 加運算式索引。只用標準 PostgreSQL（14 以上：jsonb、GIN、btree），不用擴充；
相似事故的向量用到 pgvector 時是「有就用」（DB.has_vector）。表名、SQL 只出現在各功能的 store/ 與 common/。

連線設定都在 common/config/settings.py（環境變數）：AIOPS_PG_DSN（必填）、AIOPS_PG_SCHEMA、AIOPS_PG_CONNECT_TIMEOUT、
AIOPS_PG_STATEMENT_TIMEOUT、AIOPS_PG_APP_NAME（選填）。第一次查詢才連線（import、建 Repos 都不連）。
"""
from __future__ import annotations

import json
import math
import random
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from common.clock import stored_time
from common.config.settings import settings
from common.infra.journal import Journal, JournalUnwritable, db_key, is_write


def _clean(v):
    """寫進 jsonb 之前：NaN、inf 變 null（PostgreSQL 的 jsonb 不收），datetime 轉 ISO 字串，numpy 數字轉 Python 的。"""
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_clean(x) for x in v]
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):     # numpy 的 int64、float64、bool_
        return _clean(v.item())
    return v


def dumps(doc) -> str:
    return json.dumps(_clean(doc), default=str, ensure_ascii=False)


def connect_with_retry(dsn: str, **kw):
    """連線；連不上（DB 重啟、主從切換、網路抖一下）就等 1、2、4、8、8… 秒（±20% 抖動，免得一堆工作同時重連）再試，
    總共最多 AIOPS_PG_RETRY_SECONDS 秒（預設 30）；還是不行才丟出最後一次的錯。連線被拒絕只要幾毫秒，不等的話 0.6 秒就失敗。"""
    env = settings()
    budget, delay, t0 = max(0, env.aiops_pg_retry_seconds), 1.0, time.monotonic()
    kw.setdefault("application_name", env.aiops_pg_app_name)
    while True:
        try:
            return psycopg.connect(dsn, connect_timeout=env.aiops_pg_connect_timeout, **kw)
        except psycopg.OperationalError:
            left = budget - (time.monotonic() - t0)
            if left <= 0:
                raise
            time.sleep(min(delay * random.uniform(0.8, 1.2), left))
            delay = min(delay * 2, 8.0)


class DB:
    """一條 PostgreSQL 連線（autocommit：每個語句自己就是一個交易，跟 ES 一筆一筆寫的語意一樣）。第一次用才連；斷了自動重連一次。"""

    def __init__(self, dsn: str | None = None, schema: str | None = None):
        env = settings()
        self._dsn, self._schema = dsn, schema if schema is not None else env.aiops_pg_schema
        self._conn = None
        self._lock = threading.Lock()
        self._vector: bool | None = None
        self.ensured: set[str] = set()       # 這條連線已經建過（CREATE IF NOT EXISTS）的表，不重複送 DDL
        self._journal = None                 # 本機日誌（AIOPS_JOURNAL_DIR）：第一次寫入才開
        self._journal_dir = env.aiops_journal_dir

    @property
    def dsn(self) -> str:
        return self._dsn or settings().require_pg_dsn()

    def _connect(self):
        env = settings()
        conn = connect_with_retry(self.dsn, autocommit=True, row_factory=dict_row)
        if env.aiops_pg_statement_timeout:
            conn.execute(f"SET statement_timeout = {int(env.aiops_pg_statement_timeout) * 1000}")
        if self._schema:
            conn.execute(f'SET search_path TO "{self._schema}", public')
        return conn

    @property
    def conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
        return self._conn

    def _run(self, fn):
        with self._lock:
            conn = self.conn                     # 連不上：connect_with_retry 已經等過，直接丟
            try:
                return fn(conn)
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                # 連線斷了（DB 重啟、主從切換、閒置被踢）：重連（照 connect_with_retry 等）再跑一次。
                # 連線還好好的（查詢逾時 QueryCanceled 也是 OperationalError）就照丟，不重跑長查詢
                if isinstance(e, psycopg.errors.QueryCanceled) or (not conn.closed and not conn.broken):
                    raise
                self._conn = None
                return fn(self.conn)

    def _apply(self, sql: str, params, many: bool):
        if many:
            return self._run(lambda c: c.cursor().executemany(sql, params))
        return self._run(lambda c: c.execute(sql, params).rowcount)

    @property
    def journal(self):
        """本機日誌（common/infra/journal.py）；沒設 AIOPS_JOURNAL_DIR 回 None。第一次用時先補寫別的程式留下的。"""
        if self._journal is None and self._journal_dir:
            self._journal = Journal(self._journal_dir, db_key(self.dsn, self._schema))
        return self._journal

    def recover_journal(self) -> int:
        """補寫別的程式留下、還沒寫進資料庫的（排程器每分鐘叫；每個程式第一次寫入前也會叫）。沒開日誌回 0。"""
        j = self.journal
        return j.recover(self._apply) if j is not None else 0

    def _write(self, sql: str, params, many: bool):
        j = None
        if is_write(sql):
            try:
                j = self.journal
            except OSError as e:                 # 日誌的資料夾建不起來：不要讓本機那一份變成新的故障點，直接寫資料庫
                self._journal_broken(e)
        if j is None:
            return self._apply(sql, params, many)
        if not j.recovered:
            j.recovered = True
            j.recover(self._apply)
        try:
            return j.write(self._apply, sql, params, many)
        except JournalUnwritable as e:           # 本機寫不進去（沒權限、磁碟滿）：照樣寫資料庫，只是這一筆沒有本機那一份
            self._journal_broken(e.__cause__ or e)
            return self._apply(sql, params, many)

    def _journal_broken(self, e) -> None:
        if not getattr(self, "_warned", False):
            self._warned = True
            print(f"  （本機日誌寫不進去：{type(e).__name__}: {e}；直接寫資料庫，資料庫斷線時這些寫入不會留本機一份）", flush=True)

    def execute(self, sql: str, params=None) -> int:
        """回影響的列數。INSERT／UPDATE／DELETE 有開本機日誌時先記下來再寫。"""
        return self._write(sql, params, False)

    def query(self, sql: str, params=None) -> list[dict]:
        return self._run(lambda c: c.execute(sql, params).fetchall())

    def executemany(self, sql: str, rows: list) -> None:
        if rows:
            self._write(sql, rows, True)

    def has_vector(self) -> bool:
        """pgvector 裝了沒（公司的 PostgreSQL 不一定准裝）：沒有的話向量存 jsonb、在程式裡算相似度。"""
        if self._vector is None:
            try:
                self._vector = bool(self.query("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
            except Exception:  # noqa: BLE001
                self._vector = False
        return self._vector

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


LOCK_SPACE = 0x41494F50   # advisory lock 的第一個鍵（"AIOP"）：跟同一個資料庫裡別的系統的鎖分開


@contextmanager
def job_lock(name: str):
    """同一個工作同一時間只能有一份在跑（interface/jobs/runner.run_once；docs/pdca/meta/2026-11-job-lock.md）。

    yield True：搶到了，跑；False：別人（另一個排程器、worker、換版時新舊兩隻 Pod）正在跑，這輪跳過。
    用 PostgreSQL 的 advisory lock，鎖跟著一條專用連線：工作跑完關掉連線就放掉；跑的程式死掉、連線斷掉，PostgreSQL 也會自動放掉，
    不會留下卡住的鎖。連線開 TCP keepalive（約 1 分鐘認出對方不見），網路斷掉時鎖不會一直卡著。
    AIOPS_PG_DSN 沒設（本機沒有資料庫）就不鎖、yield True；設了但連不到，丟出例外（呼叫端決定這輪不跑）。"""
    env = settings()
    if not env.aiops_pg_dsn:
        yield True
        return
    conn = connect_with_retry(env.aiops_pg_dsn, autocommit=True, application_name=f"{env.aiops_pg_app_name}-lock:{name}"[:63], keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
    try:
        got = conn.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s)) AS ok", (LOCK_SPACE, f"job:{name}")).fetchone()[0]
        yield bool(got)
    finally:
        conn.close()


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"表名只能是英數與底線：{name!r}")
    return name


class PgRepository:
    """一張表的讀寫。子類別給 TABLE、TIME（文件裡哪個欄位放進 ts）、INDEXES（額外的運算式索引，例如 "((doc->>'series'))"）。
    對外的方法照原本 ES 版的 repository（呼叫端不用改）；讀回來的是文件 dict。"""
    TABLE: str = ""
    TIME: str | None = None
    INDEXES: tuple[str, ...] = ()
    PRUNE_WHERE = "ts < %s"          # detector prune 刪哪些（參數是 cutoff）；告警改成「已結案、結案時間早於 cutoff」

    def __init__(self, es=None, scenario=None, db: DB | None = None):
        self.es, self.scenario = es, scenario
        self.db = db if db is not None else DB()

    @property
    def table(self) -> str:
        return _ident(self.TABLE)

    # ── 表 ──
    def ddl(self) -> list[str]:
        """建這張表要送的 DDL（表＋索引）。子類別要多欄位、多索引就改這裡。"""
        t = self.table
        return [f"CREATE TABLE IF NOT EXISTS {t} (id text PRIMARY KEY, ts timestamptz, doc jsonb NOT NULL, "
                f"updated_at timestamptz NOT NULL DEFAULT now())",
                f"CREATE INDEX IF NOT EXISTS {t}_ts ON {t} (ts)",
                *[f"CREATE INDEX IF NOT EXISTS {t}_x{i} ON {t} {expr}" for i, expr in enumerate(self.INDEXES)]]

    def missing(self) -> bool:
        """表還沒建（to_regclass 只要 schema 的 USAGE 權限，不用 CREATE）。"""
        return self.db.query("SELECT to_regclass(%s) IS NULL AS missing", (self.table,))[0]["missing"]

    def ensure(self, *_a, force: bool = False, **_k) -> None:
        """表已經在了就什麼都不送（engine 只要讀寫權限：DBA 先跑 detector db init 建好）；還沒建才送 DDL（那時才要 CREATE 權限）。
        同一條連線只查一次。force：不管在不在都送一次（db init 用：補上新加的索引）。
        PostgreSQL 的 CREATE TABLE IF NOT EXISTS 會先檢查 schema 的 CREATE 權限，表在不在都一樣，所以不能直接送（docs/pdca/meta/2026-11-pg-resilience.md）。"""
        if self.table in self.db.ensured and not force:
            return
        if force or self.missing():
            for stmt in self.ddl():
                self.db.execute(stmt)
        self.db.ensured.add(self.table)

    def _ts(self, doc: dict) -> datetime | None:
        return stored_time(doc.get(self.TIME)) if self.TIME else None

    # ── 讀 ──
    def _select(self, where: str = "TRUE", params=(), order: str | None = None, limit: int | None = None) -> list[tuple[str, dict]]:
        """[(id, 文件)]。表還不存在（沒人寫過）當空的。where、order 是這個 repository 自己寫的 SQL 片段，值一律走 params。"""
        self.ensure()
        sql = f"SELECT id, doc FROM {self.table} WHERE {where}" + (f" ORDER BY {order}" if order else "") + (f" LIMIT {int(limit)}" if limit else "")
        return [(r["id"], r["doc"]) for r in self.db.query(sql, params)]

    def _get(self, _id: str) -> dict | None:
        got = self._select("id = %s", (_id,))
        return got[0][1] if got else None

    def _mget(self, ids: list[str], fields: list[str] | None = None) -> dict[str, dict]:
        """{id: 文件}，只回找得到的；fields：只留這些欄位（跟 ES 的 _source_includes 一樣，頂層欄位）。"""
        if not ids:
            return {}
        rows = self._select("id = ANY(%s)", (list(ids),))
        return {i: ({k: d[k] for k in fields if k in d} if fields else d) for i, d in rows}

    def count(self, where: str = "TRUE", params=()) -> int:
        self.ensure()
        return int(self.db.query(f"SELECT count(*) AS n FROM {self.table} WHERE {where}", params)[0]["n"])

    # ── 寫 ──
    def _upsert_sql(self) -> str:
        return (f"INSERT INTO {self.table} (id, ts, doc) VALUES (%s, %s, %s::jsonb) "
                f"ON CONFLICT (id) DO UPDATE SET ts = EXCLUDED.ts, doc = EXCLUDED.doc, updated_at = now()")

    def _put(self, _id: str, doc: dict) -> None:
        self.ensure()
        self.db.execute(self._upsert_sql(), (_id, self._ts(doc), dumps(doc)))

    def bulk(self, docs: list[tuple[str, dict]], index: str | None = None) -> None:
        """(id, 文件)；同 id 覆蓋。index 參數是 ES 版留下的，這裡不用。"""
        if not docs:
            return
        self.ensure()
        last = {}
        for _id, doc in docs:          # 同一批裡同一個 id 寫兩次：留最後一筆（ES 的 bulk 也是後面蓋前面）
            last[_id] = doc
        self.db.executemany(self._upsert_sql(), [(i, self._ts(d), dumps(d)) for i, d in last.items()])

    def _patch(self, _id: str, doc: dict, upsert: dict | None = None) -> None:
        """改頂層的幾個欄位（jsonb 的 ||，跟 ES 的 partial update 一樣只合併頂層）；upsert：還沒有這筆時整筆建。"""
        self.ensure()
        ts = self._ts(doc) if self.TIME and self.TIME in doc else None
        n = self.db.execute(f"UPDATE {self.table} SET doc = doc || %s::jsonb, ts = COALESCE(%s, ts), updated_at = now() WHERE id = %s",
                            (dumps(doc), ts, _id))
        if n == 0:
            if upsert is None:
                raise KeyError(f"{self.table} 沒有 {_id}")
            self.db.execute(f"INSERT INTO {self.table} (id, ts, doc) VALUES (%s, %s, %s::jsonb) ON CONFLICT (id) DO UPDATE "
                            f"SET doc = {self.table}.doc || EXCLUDED.doc, updated_at = now()", (_id, self._ts(upsert), dumps(upsert)))

    def _delete(self, _id: str) -> None:
        self.ensure()
        self.db.execute(f"DELETE FROM {self.table} WHERE id = %s", (_id,))

    def delete_where(self, where: str, params=()) -> int:
        self.ensure()
        return self.db.execute(f"DELETE FROM {self.table} WHERE {where}", params)

    # ── 過期資料（detector prune；common/infra/prune.py）──
    def prune(self, cutoff: datetime) -> int:
        return self.delete_where(self.PRUNE_WHERE, (cutoff,))

    def prune_count(self, cutoff: datetime) -> int:
        return self.count(self.PRUNE_WHERE, (cutoff,))

    def prune_rows(self, cutoff: datetime, limit: int) -> list[dict]:
        """要刪的、最舊的 limit 筆：[{id, ts, doc}]（打包用，寫好 aiops_packed 再 delete_ids）。"""
        self.ensure()
        return self.db.query(f"SELECT id, ts, doc FROM {self.table} WHERE {self.PRUNE_WHERE} ORDER BY ts NULLS FIRST, id LIMIT {int(limit)}",
                             (cutoff,))

    def delete_ids(self, ids: list[str]) -> int:
        return self.delete_where("id = ANY(%s)", (list(ids),)) if ids else 0

    def restore(self, rows: list[tuple[str, datetime | None, dict]]) -> None:
        """打包的資料讀回（detector db restore）：照原本的 id、ts 寫回；同 id 覆蓋。"""
        if rows:
            self.ensure()
            self.db.executemany(self._upsert_sql(), [(i, ts, dumps(d)) for i, ts, d in rows])


def has(field: str) -> str:
    """「有這個欄位、而且不是 null」的 SQL（ES 的 exists）：jsonb 的 ? 對 null 也會成立，所以看型別。field 是頂層欄位名（程式裡寫死的）。"""
    if not field.replace("_", "").replace("@", "").isalnum():
        raise ValueError(f"欄位名不對：{field!r}")
    return f"COALESCE(jsonb_typeof(doc->'{field}'), 'null') <> 'null'"

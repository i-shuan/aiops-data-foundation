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
import threading
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from common.clock import stored_time
from common.config.settings import settings


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


class DB:
    """一條 PostgreSQL 連線（autocommit：每個語句自己就是一個交易，跟 ES 一筆一筆寫的語意一樣）。第一次用才連；斷了自動重連一次。"""

    def __init__(self, dsn: str | None = None, schema: str | None = None):
        env = settings()
        self._dsn, self._schema = dsn, schema if schema is not None else env.aiops_pg_schema
        self._conn = None
        self._lock = threading.Lock()
        self._vector: bool | None = None
        self.ensured: set[str] = set()       # 這條連線已經建過（CREATE IF NOT EXISTS）的表，不重複送 DDL

    @property
    def dsn(self) -> str:
        return self._dsn or settings().require_pg_dsn()

    def _connect(self):
        env = settings()
        conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row, connect_timeout=env.aiops_pg_connect_timeout,
                               application_name=env.aiops_pg_app_name)
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
            try:
                return fn(self.conn)
            except (psycopg.OperationalError, psycopg.InterfaceError):   # 連線斷了（DB 重啟、閒置被踢）：重連一次再試；其他錯照丟
                self._conn = None
                return fn(self.conn)

    def execute(self, sql: str, params=None) -> int:
        """回影響的列數。"""
        return self._run(lambda c: c.execute(sql, params).rowcount)

    def query(self, sql: str, params=None) -> list[dict]:
        return self._run(lambda c: c.execute(sql, params).fetchall())

    def executemany(self, sql: str, rows: list) -> None:
        if rows:
            self._run(lambda c: c.cursor().executemany(sql, rows))

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
    def ensure(self, *_a, **_k) -> None:
        """沒有就建（含索引）。同一條連線只送一次 DDL。舊的 ES 版有 extra_fields 參數（補 mapping），這裡不需要、忽略。"""
        if self.table in self.db.ensured:
            return
        t = self.table
        self.db.execute(f"CREATE TABLE IF NOT EXISTS {t} (id text PRIMARY KEY, ts timestamptz, doc jsonb NOT NULL, "
                        f"updated_at timestamptz NOT NULL DEFAULT now())")
        self.db.execute(f"CREATE INDEX IF NOT EXISTS {t}_ts ON {t} (ts)")
        for i, expr in enumerate(self.INDEXES):
            self.db.execute(f"CREATE INDEX IF NOT EXISTS {t}_x{i} ON {t} {expr}")
        self.db.ensured.add(t)

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

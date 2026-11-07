"""e2e 腳本用的轉接：engine 自己的資料搬到 PostgreSQL 之後，e2e 腳本原本直接查 ES 的 aiops-*（看寫了什麼、清掉重跑）。

    es = AiopsES(ES())     # 原本是 es = ES()

送到 aiops-*（以前的索引名）的請求轉到 PostgreSQL 對應的表（common/infra/es_import.LEGACY），其他（原始資料、traces-merged-*）照送真的 ES。
只支援 e2e 用到的那一小部分查詢：match_all、term、terms、prefix、range、exists、ids、bool（filter／must／should／must_not）、
size、sort（一個欄位）、_source（只留頂層欄位）；_search、_count、_delete_by_query、_doc 的 GET／PUT／DELETE、_update、
整個索引的 DELETE（清空表）；_refresh、_open、_close、_mapping、建索引是空動作。不支援的丟 NotImplementedError（寫錯一眼看得出來）。
**只給測試用**：正式程式的查詢在各 repository 裡（SQL）。
"""
from __future__ import annotations


from common.infra.es_import import LEGACY
from common.infra.repos import Repos
from common.scenario.mapping import MappingRepository

# 以前的 ES 索引名（e2e 腳本拿來組路徑；AiopsES 認得、轉到 PostgreSQL）
ANOMALY_INDEX, BASELINE_INDEX, ALERT_INDEX, INCIDENT_INDEX = "aiops-anomalies", "aiops-baseline", "aiops-alerts", "aiops-incidents"
FEEDBACK_INDEX, TUNING_INDEX, STATE_INDEX, SEGMENT_INDEX = "aiops-feedback", "aiops-tuning", "aiops-state", "aiops-trace-segments"
ANOMALY_MAPPING = TUNING_MAPPING = {}       # 以前的 ES mapping：PostgreSQL 的表不用，ensure_index 收到也只是建表


def _path(field: str) -> str:
    """keys.service → {keys,service}（jsonb 的路徑）。@timestamp 照舊。"""
    return "{" + ",".join(p.replace('"', "") for p in field.split(".")) + "}"


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _paths(field: str) -> list[str]:
    """一個欄位在文件裡可能的路徑：全部巢狀（keys → service），或第一層之後是一整個帶點的鍵（keys → "resource.attributes.service.name"，
    以前 ES 的 flattened 欄位這樣存）。"""
    parts = field.split(".")
    out = [_path(field)]
    if len(parts) > 2:
        out.append("{" + parts[0] + "," + ".".join(parts[1:]).replace('"', "") + "}")
    return out


class _Q:
    """ES 的 query（一小部分）→ (SQL, params)。"""

    def __init__(self):
        self.params: list = []

    def p(self, v) -> str:
        self.params.append(v)
        return "%s"

    def text(self, field: str) -> str:
        return "COALESCE(" + ", ".join(f"doc #>> {self.p(x)}" for x in _paths(field)) + ")"

    def jsonv(self, field: str) -> str:
        return "COALESCE(" + ", ".join(f"doc #> {self.p(x)}" for x in _paths(field)) + ")"

    def elems(self, field: str) -> str:
        """陣列欄位的每個元素（不是陣列就沒有）。"""
        j = self.jsonv(field)
        return f"jsonb_array_elements_text(CASE WHEN jsonb_typeof({j}) = 'array' THEN {self.jsonv(field)} ELSE '[]'::jsonb END)"

    def sql(self, q: dict | None) -> str:
        if not q or "match_all" in q:
            return "TRUE"
        (kind, arg), = q.items()
        if kind == "bool":
            parts = [self.sql(x) for k in ("filter", "must") for x in _list(arg.get(k))]
            should = [self.sql(x) for x in _list(arg.get("should"))]
            if should:
                parts.append("(" + " OR ".join(should) + ")")
            parts += [f"NOT ({self.sql(x)})" for x in _list(arg.get("must_not"))]
            return "(" + " AND ".join(parts or ["TRUE"]) + ")"
        if kind == "ids":
            return f"id = ANY({self.p(list(arg['values']))})"
        if kind == "exists":
            return f"COALESCE(jsonb_typeof({self.jsonv(arg['field'])}), 'null') <> 'null'"
        (field, val), = arg.items()
        if field == "_id":
            return f"id = {self.p(val)}" if kind == "term" else f"id = ANY({self.p(list(val))})"
        if kind in ("term", "terms", "prefix"):
            val = val["value"] if isinstance(val, dict) and kind != "terms" else val
            if kind == "prefix":
                like = str(val).replace("%", "").replace("_", "\\_") + "%"
                cond = lambda e: f"{e} LIKE {self.p(like)}"
            elif kind == "term":
                v = str(val).lower() if isinstance(val, bool) else str(val)
                cond = lambda e: f"{e} = {self.p(v)}"
            else:
                vals = [str(v) for v in val]
                cond = lambda e: f"{e} = ANY({self.p(vals)})"
            return f"({cond(self.text(field))} OR EXISTS (SELECT 1 FROM {self.elems(field)} AS e WHERE {cond('e')}))"
        if kind == "range":
            ops = {"gte": ">=", "gt": ">", "lte": "<=", "lt": "<"}
            out = []
            for op, v in val.items():
                if op not in ops:
                    continue
                cast = "numeric" if _num(v) else "timestamptz"
                out.append(f"({self.text(field)})::{cast} {ops[op]} {self.p(v)}")
            return "(" + " AND ".join(out or ["TRUE"]) + ")"
        raise NotImplementedError(f"AiopsES 不支援 {kind}")


def _list(v):
    return v if isinstance(v, list) else ([v] if v else [])


class AiopsES:
    def __init__(self, es, db=None):
        self._es = es
        self._repos = Repos(es, None, db=db)

    def __getattr__(self, name):            # url、s、bulk_index… 交給真的 ES
        return getattr(self._es, name)

    def repo(self, index: str):
        name = LEGACY[index]
        return MappingRepository(db=self._repos.db) if name == "mapping" else getattr(self._repos, name)

    @staticmethod
    def _aiops(index: str) -> bool:
        return index in LEGACY

    def ensure_index(self, name, body=None):
        if self._aiops(name):
            return self.repo(name).ensure()
        return self._es.ensure_index(name, body)

    def bulk_index(self, index, docs, chunk: int = 2000):
        if self._aiops(index):
            return self.repo(index).bulk(list(docs))
        return self._es.bulk_index(index, docs, chunk)

    def search(self, index, body):
        if self._aiops(index):
            return self._search(index, body)
        return self._es.search(index, body)

    def req(self, method, path, body=None, ok=(200, 201)):
        bare = path.split("?")[0].strip("/")
        parts = bare.split("/")
        if not self._aiops(parts[0]):
            return self._es.req(method, path, body, ok=ok)
        index, rest = parts[0], parts[1:]
        r = self.repo(index)
        if not rest:
            if method == "DELETE":
                r.delete_where("TRUE")
            else:
                r.ensure()
            return {"acknowledged": True}
        op = rest[0]
        if op == "_close":            # 模擬「讀不到」：表改名，下一次讀的人看到的是新建的空表
            r.ensure()
            self._repos.db.execute(f"DROP TABLE IF EXISTS {r.TABLE}_closed")
            self._repos.db.execute(f"ALTER TABLE {r.TABLE} RENAME TO {r.TABLE}_closed")
            self._repos.db.ensured.discard(r.TABLE)
            return {}
        if op == "_open":
            self._repos.db.execute(f"DROP TABLE IF EXISTS {r.TABLE}")
            self._repos.db.execute(f"ALTER TABLE IF EXISTS {r.TABLE}_closed RENAME TO {r.TABLE}")
            self._repos.db.ensured.discard(r.TABLE)
            return {}
        if op in ("_refresh", "_mapping", "_settings", "_flush"):
            return {}
        if op == "_search":
            return self._search(index, body or {})
        if op == "_count":
            q = _Q()
            return {"count": r.count(q.sql((body or {}).get("query")), q.params)}
        if op == "_delete_by_query":
            q = _Q()
            return {"deleted": r.delete_where(q.sql((body or {}).get("query")), q.params), "failures": []}
        if op == "_doc" and len(rest) == 2:
            _id = rest[1]
            if method == "GET":
                got = r._get(_id)
                return {"_id": _id, "found": got is not None, **({"_source": got} if got is not None else {})}
            if method in ("PUT", "POST"):
                r._put(_id, body)
                return {"result": "created"}
            if method == "DELETE":
                r._delete(_id)
                return {"result": "deleted"}
        if op == "_update" and len(rest) == 2:
            r._patch(rest[1], body.get("doc") or {}, body.get("upsert"))
            return {"result": "updated"}
        raise NotImplementedError(f"AiopsES 不支援 {method} {path}")

    def _search(self, index: str, body: dict) -> dict:
        if body.get("aggs") or body.get("aggregations"):
            raise NotImplementedError("AiopsES 不支援 aggregation：把資料讀回來在 Python 裡算")
        r, q = self.repo(index), _Q()
        where = q.sql(body.get("query"))
        order = "id"
        for s in _list(body.get("sort")):
            (f, d), = (s.items() if isinstance(s, dict) else [(s, "asc")])
            d = d.get("order", "asc") if isinstance(d, dict) else d
            if f not in ("_doc", "_id"):
                order = f"doc #>> '{_path(f)}' {'DESC' if d == 'desc' else 'ASC'} NULLS LAST, id"
            break
        rows = r._select(where, q.params, order=order, limit=int(body.get("size", 10)) or None)
        src = body.get("_source")
        if isinstance(src, list):
            rows = [(i, {k: d[k] for k in src if k in d}) for i, d in rows]
        return {"hits": {"total": {"value": len(rows)}, "hits": [{"_id": i, "_index": index, "_source": d} for i, d in rows]}}

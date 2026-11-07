"""事故的向量（PostgreSQL 的 aiops_incident_vectors）：一件確認過的事故一筆，id 跟事故一樣。
doc：{model, hash（算向量的文字的雜湊，事故改了就重算）, vector}。pgvector 有裝就另外寫一欄 embedding（vector 型別），
最近的幾件用 SQL 的 <=> 算；沒裝就把向量讀回來在程式裡算（事故幾十到幾百件，不慢）。"""
from __future__ import annotations

from common.infra.pg import PgRepository
from common.types.incident import INCIDENT_TABLE

VECTOR_TABLE = "aiops_incident_vectors"


def _literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


class IncidentVectorRepository(PgRepository):
    TABLE = VECTOR_TABLE

    def ensure(self, *_a, **_k) -> None:
        first = self.table not in self.db.ensured
        super().ensure()
        if first and self.db.has_vector():
            self.db.execute(f"ALTER TABLE {self.table} ADD COLUMN IF NOT EXISTS embedding vector")

    def known(self, model: str) -> dict[str, str]:
        """{事故 id: 文字雜湊}：這個模型已經算過的。"""
        return {i: d.get("hash") for i, d in self._select("doc->>'model' = %s", (model,))}

    def put_many(self, rows: list[tuple[str, str, str, list[float]]]) -> None:
        """[(事故 id, 模型, 文字雜湊, 向量)]。"""
        if not rows:
            return
        self.ensure()
        docs = [(i, {"model": m, "hash": h, "vector": v}) for i, m, h, v in rows]
        self.bulk(docs)
        if self.db.has_vector():
            self.db.executemany(f"UPDATE {self.table} SET embedding = %s::vector WHERE id = %s", [(_literal(v), i) for i, _, _, v in rows])

    def vectors(self, model: str, ids: list[str]) -> dict[str, list[float]]:
        if not ids:
            return {}
        return {i: d.get("vector") or [] for i, d in self._select("doc->>'model' = %s AND id = ANY(%s)", (model, list(ids)))}

    def nearest_sql(self, model: str, query: list[float], statuses: tuple[str, ...], exclude_id: str | None, k: int) -> list[tuple[str, float]]:
        """pgvector 版：[(事故 id, 相似度)]，跟確認過的事故 join（狀態對的才算）。沒裝 pgvector 不要叫這個。"""
        self.ensure()
        sql = (f"SELECT v.id, 1 - (v.embedding <=> %s::vector) AS sim FROM {self.table} v JOIN {INCIDENT_TABLE} i ON i.id = v.id "
               f"WHERE v.doc->>'model' = %s AND v.embedding IS NOT NULL AND i.doc->>'status' = ANY(%s)"
               + (" AND v.id <> %s" if exclude_id else "") + " ORDER BY v.embedding <=> %s::vector LIMIT %s")
        lit = _literal(query)
        params = [lit, model, list(statuses), *([exclude_id] if exclude_id else []), lit, int(k)]
        return [(r["id"], float(r["sim"])) for r in self.db.query(sql, params)]




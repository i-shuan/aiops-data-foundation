"""異常總表的歸屬：dept、group（告警要依部門分送，docs/pdca/phase2-anomaly/2026-10-anomaly-owner.md）。

資料本身早就有（Collector 讀 namespace 的 dept label、算出 group，metrics／trace 在 resource.attributes.dept、.group）；
異常算出來寫回 aiops-anomalies 時沒帶，這裡補上：
  1. 每次執行去 metrics 查一次「namespace → dept、group」「服務 → dept、group」（最近 OWNER_DAYS 天）
  2. 每筆異常依 keys 裡的 namespace（優先，dept 是 namespace 的 label）或服務對回去
  3. 找不到的記 group = unassigned、dept 空，跟 Collector 對「沒有 dept」的處理一樣
"""
from __future__ import annotations

from .es import ANOMALY_INDEX

OWNER_DAYS = 2
UNASSIGNED = "unassigned"

NS_FIELD, SVC_FIELD = "resource.attributes.k8s.namespace.name", "resource.attributes.service.name"
DEPT_FIELD, GROUP_FIELD = "resource.attributes.dept", "resource.attributes.group"

# 異常的 keys 裡，哪些欄位是 namespace、哪些是服務（baseline、規則、peer、quality、trace 的寫法都不一樣）
NS_KEYS = ("resource.attributes.k8s.namespace.name", "k8s.namespace.name", "namespace", "kubernetes.namespace")
SVC_KEYS = ("resource.attributes.service.name", "service", "entry.service", "service.name", "caller", "callee")


def _agg(field: str) -> dict:
    return {"terms": {"field": field, "size": 10000},
            "aggs": {"d": {"terms": {"field": DEPT_FIELD, "size": 1}}, "g": {"terms": {"field": GROUP_FIELD, "size": 1}}}}


def owners_from_agg(res: dict) -> dict:
    """查詢結果 → {("ns", 名稱): (dept, group), ("svc", 名稱): (dept, group)}。"""
    out = {}
    for kind, name in (("ns", "ns"), ("svc", "svc")):
        for b in res.get("aggregations", {}).get(name, {}).get("buckets", []):
            d, g = b["d"]["buckets"], b["g"]["buckets"]
            out[(kind, b["key"])] = (d[0]["key"] if d else None, g[0]["key"] if g else None)
    return out


def load_owners(es, days: int = OWNER_DAYS) -> dict:
    body = {"size": 0, "query": {"range": {"@timestamp": {"gte": f"now-{days}d"}}},
            "aggs": {"ns": _agg(NS_FIELD), "svc": _agg(SVC_FIELD)}}
    res = es.req("POST", "/metrics-*.otel-*/_search?ignore_unavailable=true&allow_no_indices=true", body)
    return owners_from_agg(res)


def owner_of(keys: dict, owners: dict) -> tuple[str | None, str]:
    """(dept, group)。namespace 優先（dept 是 namespace 的 label），其次服務；找不到 → (None, unassigned)。"""
    for kind, fields in (("ns", NS_KEYS), ("svc", SVC_KEYS)):
        for f in fields:
            v = (keys or {}).get(f)
            if v is not None and (kind, v) in owners:
                dept, group = owners[(kind, v)]
                if group or dept:
                    return dept, group or UNASSIGNED
    return None, UNASSIGNED


def write_anomalies(es, docs: list) -> None:
    """寫進異常總表，每筆帶上 dept、group。docs：(_id, 文件)。"""
    if not docs:
        return
    owners = load_owners(es)
    for _, d in docs:
        d["dept"], d["group"] = owner_of(d.get("keys"), owners)
    es.bulk_index(ANOMALY_INDEX, docs)

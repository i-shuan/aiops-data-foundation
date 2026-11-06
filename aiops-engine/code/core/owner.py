"""⑤ 異常總表的歸屬：dept、group（告警要依部門分送，docs/pdca/phase2-anomaly/2026-10-anomaly-owner.md）。

資料本身早就有（Collector 讀 namespace 的 dept label、算出 group，metrics／trace 在 resource.attributes.dept、.group；
APM 是 agent 設的 labels.dept、labels.group）；異常算出來寫回 aiops-anomalies 時沒帶，sources 的 ESStore.write_anomalies 用這裡的函式補上：
  1. 每次執行去 metrics 查一次「namespace → dept、group」「服務 → dept、group」（最近 OWNER_DAYS 天）；
     查哪個索引、哪幾個欄位依資料來源（core/source.py 的 owner）
  2. 每筆異常依 keys 裡的 namespace（優先，dept 是 namespace 的 label）或服務對回去
  3. 找不到的記 group = unassigned、dept 空，跟 Collector 對「沒有 dept」的處理一樣
"""
from __future__ import annotations

from . import plugin, source

OWNER_DAYS = 2
UNASSIGNED = "unassigned"

# 異常的 keys 裡，哪些欄位是 namespace；哪些是「對象」（服務／機台）由 plugin 決定（core/plugin.py 的 entity_keys）
NS_KEYS = ("resource.attributes.k8s.namespace.name", "k8s.namespace.name", "namespace", "kubernetes.namespace")


def _agg(field: str, f: dict) -> dict:
    return {"terms": {"field": field, "size": 10000},
            "aggs": {"d": {"terms": {"field": f["dept"], "size": 1}}, "g": {"terms": {"field": f["group"], "size": 1}}}}


def owners_from_agg(res: dict) -> dict:
    """查詢結果 → {("ns", 名稱): (dept, group), ("svc", 名稱): (dept, group)}。"""
    out = {}
    for kind, name in (("ns", "ns"), ("svc", "svc")):
        for b in res.get("aggregations", {}).get(name, {}).get("buckets", []):
            d, g = b["d"]["buckets"], b["g"]["buckets"]
            out[(kind, b["key"])] = (d[0]["key"] if d else None, g[0]["key"] if g else None)
    return out


def load_owners(es, days: int = OWNER_DAYS) -> dict:
    f = source.profile("owner")
    # static：主檔（工廠的 eqp-master）沒有時間欄位，整份都算
    query = {"match_all": {}} if f.get("static") else {"range": {"@timestamp": {"gte": f"now-{days}d"}}}
    body = {"size": 0, "query": query, "aggs": {"ns": _agg(f["namespace"], f), "svc": _agg(f["service"], f)}}
    res = es.req("POST", f"/{f['index']}/_search?ignore_unavailable=true&allow_no_indices=true", body)
    return owners_from_agg(res)


def owner_of(keys: dict, owners: dict) -> tuple[str | None, str]:
    """(dept, group)。namespace 優先（dept 是 namespace 的 label），其次服務；找不到 → (None, unassigned)。"""
    for kind, fields in (("ns", NS_KEYS), ("svc", plugin.current().entity_keys)):
        for f in fields:
            v = (keys or {}).get(f)
            if v is not None and (kind, v) in owners:
                dept, group = owners[(kind, v)]
                if group or dept:
                    return dept, group or UNASSIGNED
    return None, UNASSIGNED

"""OTel 與 APM 並存的端到端測試：一個請求跨過兩套系統（mes-api 用 APM、wip-worker 用 OTel），
各自寫進自己的 raw 索引（格式不同），detector trace-merge 讀兩個索引、合成**同一筆** traces-merged-*，呼叫鏈接得起來。

需要自己的 ES（不要對共用 cluster 跑：會刪 mixedtest- 開頭的索引與 mixedtest- 的 merged 文件）：
  ES_URL=http://127.0.0.1:9200 ES_AUTH=x:x python3 -X utf8 -m tests.mixed_e2e      （在 aiops-engine/ 底下）
預期（先寫）：①merged 只有 1 筆、5 個 span；②mes-api 與 wip-worker 兩個服務都在；③呼叫鏈 mes-api → wip-worker → oracle 接得起來
（OTel 那邊的入口 span 的 parent 是 APM 那邊的往外呼叫 span）；④耗時單位都換成毫秒（APM 微秒、OTel 奈秒）；
⑤只看 APM 的設定（只列 APM 索引）合不到 wip-worker——證明上面的結果真的是兩個來源合出來的。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from common.infra.es import ES
from tests.aiops_es import AiopsES     # engine 自己的資料在 PostgreSQL：aiops-* 的請求轉過去
from data.store.models import from_merged, group_traces, topology
from tests import apm_synth

APM_IX, OTEL_IX, TID = "mixedtest-apm-traces", "mixedtest-otel-traces", "mixedtrace000000000000000000000001"
KEYWORDS = {"mappings": {"dynamic_templates": [{"s": {"match_mapping_type": "string", "mapping": {"type": "keyword"}}}],
                         "properties": {"@timestamp": {"type": "date"}, "event": {"properties": {"ingested": {"type": "date"}}}}}}
ok = True


def check(name: str, cond: bool, detail=None):
    global ok
    ok &= bool(cond)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond or detail is None else f"：{detail}"))


def docs(t0: datetime) -> tuple[list[dict], list[dict]]:
    ing = (t0 + timedelta(seconds=1)).isoformat()
    ts = lambda ms: (t0 + timedelta(milliseconds=ms)).isoformat()
    apm = [  # APM：mes-api 入口（transaction）、往外呼叫 wip-worker（span）；耗時是微秒
        {"@timestamp": ts(0), "processor": {"event": "transaction"}, "trace": {"id": TID}, "service": {"name": "mes-api"},
         "transaction": {"id": "tx1", "name": "GET /lots/{id}", "type": "request", "duration": {"us": 1_600_000}}, "event": {"outcome": "failure", "ingested": ing}},
        {"@timestamp": ts(3), "processor": {"event": "span"}, "trace": {"id": TID}, "service": {"name": "mes-api"}, "parent": {"id": "tx1"},
         "span": {"id": "sp1", "name": "GET wip-worker/wip", "type": "external", "duration": {"us": 1_550_000}, "destination": {"service": {"resource": "wip-worker"}}},
         "event": {"outcome": "failure", "ingested": ing}},
    ]
    otel = [  # OTel：wip-worker 入口（parent 是 APM 那邊的往外呼叫 sp1）、查 Oracle；耗時是奈秒
        {"@timestamp": ts(5), "trace_id": TID, "span_id": "ot1", "parent_span_id": "sp1", "name": "GET /wip", "kind": "Server", "duration": 1_500_000_000,
         "status": {"code": "Error"}, "resource": {"attributes": {"service.name": "wip-worker"}}, "attributes": {}, "event": {"ingested": ing}},
        {"@timestamp": ts(7), "trace_id": TID, "span_id": "ot2", "parent_span_id": "ot1", "name": "SELECT LOT_HISTORY", "kind": "Client", "duration": 1_450_000_000,
         "status": {"code": "Error"}, "resource": {"attributes": {"service.name": "wip-worker"}}, "attributes": {"peer.service": "oracle"}, "event": {"ingested": ing}},
        {"@timestamp": ts(2), "trace_id": TID, "span_id": "ot0", "parent_span_id": None, "name": "warmup", "kind": "Internal", "duration": 1_000_000,
         "status": {"code": "Unset"}, "resource": {"attributes": {"service.name": "wip-worker"}}, "attributes": {}, "event": {"ingested": ing}},
    ]
    return apm, otel


def bulk(es: ES, index: str, rows: list[dict]) -> None:
    body = "".join(json.dumps({"index": {"_index": index}}) + "\n" + json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    r = es.s.post(f"{es.url}/_bulk?refresh=true", data=body.encode("utf-8"), headers={"Content-Type": "application/x-ndjson"}, timeout=60)
    if r.json().get("errors"):
        raise SystemExit(f"寫 {index} 有錯：{r.text[:300]}")


def merge(config: str, at: datetime, source: str) -> str:
    env = {**os.environ, "AIOPS_SOURCE": source, "ES_URL": os.environ["ES_URL"]}
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "main", "detector", "trace-merge", "--config", config, "--window", "3h", "--at", at.isoformat()],
                       capture_output=True, text=True, env=env)
    return (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else ""


def merged(es: ES) -> list[dict]:
    es.req("POST", "/traces-merged-*/_refresh?ignore_unavailable=true", ok=(200, 404))
    res = es.req("POST", "/traces-merged-*/_search", {"size": 10, "query": {"term": {"trace_id": TID}}})
    return [h["_source"] for h in res["hits"]["hits"]]


def main() -> int:
    es = AiopsES(ES())
    for ix in (APM_IX, OTEL_IX):
        es.req("DELETE", f"/{ix}", ok=(200, 404))
        es.req("PUT", f"/{ix}", KEYWORDS)
    es.req("POST", "/traces-merged-*/_delete_by_query?refresh=true&ignore_unavailable=true&conflicts=proceed", {"query": {"term": {"trace_id": TID}}}, ok=(200, 404))
    es.req("PUT", "/_index_template/traces-merged", apm_synth.merged_template())
    es.req("DELETE", "/aiops-state/_doc/trace-merge", ok=(200, 404))
    t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)
    apm, otel = docs(t0)
    bulk(es, APM_IX, apm)
    bulk(es, OTEL_IX, otel)
    at = t0 + timedelta(minutes=2)

    print("== 只看 APM 的設定（對照組）")
    only_apm = "tests/mixed_apm_only.yaml"
    open(only_apm, "w", encoding="utf-8").write(f"traces_index: traces-merged-*\nraw_index: [{{index: {APM_IX}, source: apm}}]\nmerge: {{settle_seconds: 0}}\n")
    try:
        print("  " + merge(only_apm, at, "apm"))
        only = merged(es)
        check("只讀 APM 索引：merged 有 1 筆，但只有 mes-api 的 2 個 span（wip-worker 在 OTel 那邊，讀不到）",
              len(only) == 1 and sorted(s["service"] for s in only[0]["spans"]) == ["mes-api", "mes-api"], [s["service"] for s in only[0]["spans"]] if only else None)
        es.req("POST", "/traces-merged-*/_delete_by_query?refresh=true&ignore_unavailable=true&conflicts=proceed", {"query": {"term": {"trace_id": TID}}}, ok=(200, 404))
        es.req("DELETE", "/aiops-state/_doc/trace-merge", ok=(200, 404))
    finally:
        os.remove(only_apm)

    print("== 兩個來源並存（AIOPS_SOURCE=otel,apm，raw_index 兩個索引各帶 source）")
    print("  " + merge("tests/mixed_trace.yaml", at, "otel,apm"))
    both = merged(es)
    check("①merged 只有 1 筆（一個請求不是兩筆）", len(both) == 1, len(both))
    doc = both[0] if both else {"spans": []}
    spans = {s["span_id"]: s for s in doc["spans"]}
    check("①′5 個 span 都在（APM 2 個、OTel 3 個）", sorted(spans) == ["ot0", "ot1", "ot2", "sp1", "tx1"], sorted(spans))
    check("②兩個服務都在", {s["service"] for s in doc["spans"]} == {"mes-api", "wip-worker"}, {s["service"] for s in doc["spans"]})
    parents = {k: v.get("parent") for k, v in spans.items()}
    check("③呼叫鏈接得起來：tx1 → sp1（APM）→ ot1（OTel，parent 指到 APM 的 sp1）→ ot2", parents.get("sp1") == "tx1" and parents.get("ot1") == "sp1" and parents.get("ot2") == "ot1", parents)
    check("④耗時都是毫秒：APM 1_600_000 微秒 = 1600 ms、OTel 1_500_000_000 奈秒 = 1500 ms",
          abs(spans.get("tx1", {}).get("dur_ms", 0) - 1600) < 1 and abs(spans.get("ot1", {}).get("dur_ms", 0) - 1500) < 1, (spans.get("tx1", {}).get("dur_ms"), spans.get("ot1", {}).get("dur_ms")))
    check("⑤種類：APM 的 transaction 是 Server、外部呼叫是 Client；OTel 照自己的 kind",
          (spans.get("tx1", {}).get("kind"), spans.get("sp1", {}).get("kind"), spans.get("ot1", {}).get("kind")) == ("Server", "Client", "Server"),
          (spans.get("tx1", {}).get("kind"), spans.get("sp1", {}).get("kind"), spans.get("ot1", {}).get("kind")))
    edges = [(e.caller, e.callee) for e in topology(group_traces(from_merged(doc)))]
    check("⑥「誰呼叫誰」看得到 mes-api → wip-worker（跨來源的呼叫關係，事件整合的連鎖併件靠它）", ("mes-api", "wip-worker") in edges, edges)

    for ix in (APM_IX, OTEL_IX):
        es.req("DELETE", f"/{ix}", ok=(200, 404))
    es.req("POST", "/traces-merged-*/_delete_by_query?refresh=true&ignore_unavailable=true&conflicts=proceed", {"query": {"term": {"trace_id": TID}}}, ok=(200, 404))
    print("\n" + ("全部通過" if ok else "有項目失敗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

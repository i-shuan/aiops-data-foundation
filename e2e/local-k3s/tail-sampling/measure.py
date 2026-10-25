#!/usr/bin/env python3
"""尾端取樣一輪的結果：ES 裡每種請求留了幾個、完整不完整、是哪一條規則留的；trace-sampler 的資源、內部計數。

用法：ES_URL=… ES_AUTH=elastic:… python3 measure.py <RUN> <開始 epoch 秒> <結束 epoch 秒> '<SENT 那行的 JSON>' '<內部計數 JSON>'
"""
import base64
import json
import os
import sys
import urllib.request

ES = os.environ["ES_URL"]
AUTH = "Basic " + base64.b64encode(os.environ.get("ES_AUTH", "elastic:changeme-test-only").encode()).decode()


def search(index, body):
    req = urllib.request.Request(f"{ES}/{index}/_search", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def per_trace(run):
    """每個 trace：類別、幾段、哪條規則留的。composite 分頁，量大也撐得住。"""
    out, after = {}, None
    while True:
        comp = {"size": 5000, "sources": [{"t": {"terms": {"field": "trace_id"}}}]}
        if after:
            comp["after"] = after
        res = search("traces-*", {"size": 0, "query": {"term": {"attributes.lt.run": run}}, "aggs": {"x": {
            "composite": comp, "aggs": {"c": {"terms": {"field": "attributes.lt.category", "size": 1}},
                                        "p": {"terms": {"field": "scope.attributes.tailsampling.policy", "size": 1}}}}}})
        agg = res["aggregations"]["x"]
        for b in agg["buckets"]:
            c = b["c"]["buckets"][0]["key"] if b["c"]["buckets"] else "?"
            p = b["p"]["buckets"][0]["key"] if b["p"]["buckets"] else "(cache)"
            out[b["key"]["t"]] = (c, b["doc_count"], p)
        after = agg.get("after_key")
        if not after:
            return out


def resources(start, end):
    """kubelet 抓的容器 CPU（核）、記憶體（working set），trace-sampler 每台和 node Collector。"""
    rng = {"range": {"@timestamp": {"gte": int(start * 1000), "lte": int(end * 1000)}}}
    res = search("metrics-kubeletstatsreceiver.otel-*", {"size": 0, "query": {"bool": {"filter": [
        rng, {"term": {"resource.attributes.k8s.namespace.name": "otel-collector"}},
        {"exists": {"field": "metrics.container.memory.working_set"}}]}},
        "aggs": {"pod": {"terms": {"field": "resource.attributes.k8s.pod.name", "size": 20}, "aggs": {
            "mem": {"max": {"field": "metrics.container.memory.working_set"}}}}}})
    cpu = search("metrics-kubeletstatsreceiver.otel-*", {"size": 0, "query": {"bool": {"filter": [
        rng, {"term": {"resource.attributes.k8s.namespace.name": "otel-collector"}},
        {"exists": {"field": "metrics.container.cpu.usage"}}]}},
        "aggs": {"pod": {"terms": {"field": "resource.attributes.k8s.pod.name", "size": 20}, "aggs": {
            "avg": {"avg": {"field": "metrics.container.cpu.usage"}}, "max": {"max": {"field": "metrics.container.cpu.usage"}}}}}})
    out = {}
    for b in res["aggregations"]["pod"]["buckets"]:
        out.setdefault(b["key"], {})["mem_mb"] = round(b["mem"]["value"] / 1048576)
    for b in cpu["aggregations"]["pod"]["buckets"]:
        out.setdefault(b["key"], {}).update(cpu_avg=round(b["avg"]["value"], 3), cpu_max=round(b["max"]["value"], 3))
    return {k: v for k, v in out.items() if k.startswith(("trace-sampler", "otel-node"))}


def main():
    run, start, end, sent, internal = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), json.loads(sys.argv[4]), json.loads(sys.argv[5])
    traces = per_trace(run)
    print(f"  送出 {sum(sent['traces'].values()):,} 個 trace（{sent['spans']:,} 段，送失敗 {sent['failed_spans']}），"
          f"{sent['seconds']} 秒，平均每秒 {sum(sent['traces'].values()) / max(sent['seconds'] - 15, 1):.0f} 個")
    print(f"  {'種類':<10}{'送出':>8}{'留下':>8}{'比例':>8}{'不完整':>8}  留下的原因")
    for cat, n in sent["traces"].items():
        mine = [(spans, pol) for c, spans, pol in traces.values() if c == cat]
        kept, incomplete = len(mine), sum(1 for s, _ in mine if s < 4)
        pols = {}
        for _, p in mine:
            pols[p] = pols.get(p, 0) + 1
        why = "、".join(f"{p} {k}" for p, k in sorted(pols.items(), key=lambda x: -x[1]))
        print(f"  {cat:<10}{n:>8,}{kept:>8,}{kept / n if n else 0:>8.1%}{incomplete:>8}  {why}")
    print("  trace-sampler 內部計數：" + "、".join(f"{k} {v:,}" for k, v in internal.items()))
    for pod, r in sorted(resources(start, end).items()):
        print(f"  {pod}：記憶體最高 {r.get('mem_mb', '?')}MB、CPU 平均 {r.get('cpu_avg', '?')} 核、最高 {r.get('cpu_max', '?')} 核")


if __name__ == "__main__":
    main()

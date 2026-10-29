"""metrics → trace 關聯的邊緣情況：讀 ES 裡 mt-edge（metrics-trace-apps.yaml）的資料，每項印 PASS／FAIL 和量到的數字。

run-metrics-trace-e2e.sh 跑完各階段後呼叫；也可以對已經在 ES 的資料重跑（時間給一樣的）：
  ES_URL=http://127.0.0.1:31200 ES_AUTH=elastic:changeme-test-only python -X utf8 metrics_trace_check.py \\
      --normal <ISO> --incident <ISO> --heal <ISO> --crash <ISO> --delete <ISO> --end <ISO>

用的是根因分析自己的查法（investigator.es 的 entry_filter、entry_stats、sample_trace_ids，__main__ 的 _route_of），不另寫一套。
測試 cluster 的 trace-sampler 留 100%（RANDOM_PCT=100）；「正式只隨機留 5%」用 trace_id 前兩碼 < 0d（13/256 = 5.1%）
模擬：probabilistic sampler 也是看 trace_id 的雜湊，對隨機的 trace_id 效果相同。慢（≥ 1 秒）、錯的照正式規則全留。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))

from core.es import ES, time_range  # noqa: E402
from investigator import es as E  # noqa: E402
from investigator.__main__ import _route_of  # noqa: E402

MIX = "metrics-*.otel-*"
TIX = "traces-*.otel-*"
SVC = "mt-edge"
HIST = "metrics.http.server.request.duration"
W = timedelta(minutes=5)
fails = 0


def check(ok: bool, what: str, got: str):
    global fails
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {what}：{got}")


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def bucket(t_ms: float) -> int:
    return int(t_ms // 300_000)


class Sampled:
    """正式的尾端取樣：慢（≥ 1 秒）、錯的全留，其他隨機 5%。把條件加進每個 trace 查詢。"""
    KEEP = {"bool": {"should": [{"range": {"duration": {"gte": 1_000_000_000}}}, {"term": {"status.code": "Error"}},
                                {"regexp": {"trace_id": "0[0-9a-c].*"}}], "minimum_should_match": 1}}

    def __init__(self, es: ES):
        self.es = es

    def search(self, index: str, body: dict) -> dict:
        body = {**body, "query": {"bool": {"filter": [body["query"], self.KEEP]}}}
        return self.es.search(index, body)


def metric_docs(es: ES, start: datetime, end: datetime, route: str | None = None, pod: str | None = None) -> list[dict]:
    f = [time_range(start, end), {"term": {"resource.attributes.service.name": SVC}}, {"exists": {"field": HIST}}]
    if route:
        f.append({"term": {"attributes.http.route": route}})
    if pod:
        f.append({"term": {"resource.attributes.k8s.pod.name": pod}})
    hits = es.search(MIX, {"size": 5000, "sort": [{"@timestamp": "asc"}], "query": {"bool": {"filter": f}}})["hits"]["hits"]
    return [h["_source"] for h in hits]


def doc_count(d: dict) -> int:
    h = d["metrics"]["http.server.request.duration"]
    return sum(h.get("positive", {}).get("counts", [])) + sum(h.get("negative", {}).get("counts", [])) + h.get("zero", {}).get("count", 0)


def max_s(d: dict) -> float:
    """這筆直方圖最慢那格的值（秒）。ES 存成指數直方圖：第 i 格約 2^(i / 2^scale)。"""
    h = d["metrics"]["http.server.request.duration"]
    pos = h.get("positive", {})
    return max((2 ** (i / 2 ** h["scale"]) for i, c in zip(pos.get("indices", []), pos.get("counts", [])) if c), default=0.0)


def metric_count(es: ES, start: datetime, end: datetime, route: str) -> tuple[int, float]:
    """這段時間（依 metrics 的 @timestamp）這條路由的請求數、p95（秒）：跟 detector 一樣用 value_count、percentiles。"""
    r = es.search(MIX, {"size": 0, "query": {"bool": {"filter": [time_range(start, end), {"term": {"resource.attributes.service.name": SVC}},
                                                                  {"term": {"attributes.http.route": route}}]}},
                        "aggs": {"n": {"value_count": {"field": HIST}}, "p": {"percentiles": {"field": HIST, "percents": [95]}}}})
    return int(r["aggregations"]["n"]["value"]), r["aggregations"]["p"]["values"]["95.0"] or 0.0


def traces(es: ES, start: datetime, end: datetime, name: str, svc: str = SVC) -> list[dict]:
    """這段時間（依 span 開始時間）這個名稱的 Server span：開始、結束（ms）、是不是故意弄慢的（test.slow）。"""
    q = {"bool": {"filter": [time_range(start, end), *E.entry_filter(svc, name)]}}
    hits = es.search(TIX, {"size": 10000, "query": q, "_source": ["@timestamp", "duration", "trace_id", "attributes.test.slow",
                                                                    "resource.attributes.k8s.pod.name"]})["hits"]["hits"]
    out = []
    for h in hits:
        s = h["_source"]
        t0 = s["@timestamp"] if isinstance(s["@timestamp"], (int, float)) else ms(ts(s["@timestamp"]))
        out.append({"id": s["trace_id"], "start": t0, "end": t0 + s["duration"] / 1e6,
                    "slow": (s.get("attributes") or {}).get("test.slow") == "1",
                    "pod": ((s.get("resource") or {}).get("attributes") or {}).get("k8s.pod.name")})
    return out


def keys_of(d: dict) -> dict:
    """baseline／規則記異常時的 keys（by 的欄位）：服務、method、路由。"""
    a = d["attributes"]
    return {"resource.attributes.service.name": d["resource"]["attributes"]["service.name"],
            "attributes.http.request.method": a.get("http.request.method"), "attributes.http.route": a.get("http.route")}


def main() -> int:
    p = argparse.ArgumentParser()
    for k in ("normal", "incident", "heal", "crash", "delete", "end"):
        p.add_argument(f"--{k}", required=True)
    a = p.parse_args()
    t_norm, t_inc, t_heal, t_crash, t_del, t_end = (ts(getattr(a, k)) for k in ("normal", "incident", "heal", "crash", "delete", "end"))
    es = ES()
    sampled = Sampled(es)
    # 事故中第一個完整的 5 分鐘（baseline 的時間窗）
    b0 = datetime.fromtimestamp((bucket(ms(t_inc)) + 1) * 300, timezone.utc)
    assert b0 + W <= t_heal, "事故要涵蓋一個完整的 5 分鐘"

    # trace 線中途斷掉（例如 Collector 被換掉、loadbalancing 卡住）的話，後面的數字都不能用
    r = es.search(TIX, {"size": 0, "query": {"bool": {"filter": [time_range(t_norm, t_end), {"term": {"resource.attributes.service.name": SVC}}]}},
                        "aggs": {"m": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1m",
                                                          "extended_bounds": {"min": ms(t_norm) // 60000 * 60000 + 60000, "max": ms(t_end) - 60000}}}}})
    per_min = [b["doc_count"] for b in r["aggregations"]["m"]["buckets"]]
    print("== 0. trace 線整段都通")
    check(bool(per_min) and min(per_min) > 0, "每分鐘都有 mt-edge 的 trace",
          f"{len(per_min)} 分鐘，最少 {min(per_min, default=0)}、最多 {max(per_min, default=0)} 筆／分")
    if not per_min or min(per_min) == 0:
        print("\ntrace 線中途斷了，這次的結果不能用，修好再重跑")
        return 2

    print("== 1. 路由沒參數化：用 metrics 的 keys 組出的 route（_route_of）去撈 trace")
    for mroute, real_name, what in (("/items/123", "GET /items/{id}", "metrics 沒參數化（/items/123）、trace 有"),
                                    ("/orders/{id}", "GET /orders/42", "metrics 有參數化、trace 沒有（GET /orders/42）"),
                                    ("/stock/{id}", "/stock/{id}", "舊版 agent：span 名稱沒有 method（/stock/{id}）")):
        docs = metric_docs(es, b0, b0 + W, mroute)
        route = _route_of(keys_of(docs[0])) if docs else None
        got = E.entry_stats(es, TIX, SVC, route, b0, b0 + W)["requests"]
        real = E.entry_stats(es, TIX, SVC, real_name, b0, b0 + W)["requests"]
        check(bool(docs) and got == 0 and real > 0, what,
              f"_route_of → {route!r} 撈到 {got} 筆；trace 實際名稱 {real_name!r} 有 {real} 筆")
    allsvc = E.entry_stats(es, TIX, SVC, None, b0, b0 + W)["requests"]
    print(f"        退回只用服務（不限路由）撈到 {allsvc} 筆：7 條路由混在一起")

    print("== 2. Java Prometheus 名字 → OTel 名字（node Collector 改名）")
    docs = metric_docs(es, b0, b0 + W)
    routes = sorted({d["attributes"].get("http.route") for d in docs})
    codes = {type(d["attributes"].get("http.response.status_code")).__name__ for d in docs}
    cnt = lambda q: es.search(MIX, {"size": 0, "track_total_hits": True, "query": {"bool": {"filter": [  # noqa: E731
        time_range(t_norm, t_end), {"term": {"resource.attributes.service.name": SVC}}, q]}}})["hits"]["total"]["value"]
    old_name, old_attr = cnt({"exists": {"field": "metrics.http_server_request_duration_seconds"}}), cnt({"exists": {"field": "attributes.http_route"}})
    check(len(routes) == 6 and codes == {"int"} and old_name == 0 and old_attr == 0, "http_server_request_duration_seconds、http_route 改成 OTel 名字",
          f"{HIST} 的路由 {len(routes)} 條、狀態碼型別 {codes}；舊名字 {old_name} 筆、http_route {old_attr} 筆")
    mm = cnt({"exists": {"field": "metrics.http_server_requests_seconds"}})
    mm_uri = cnt({"term": {"attributes.uri": "/legacy/{id}"}})
    mm_rule = cnt({"bool": {"filter": [{"exists": {"field": HIST}}, {"term": {"attributes.uri": "/legacy/{id}"}}]}})
    check(mm > 0 and mm_uri > 0 and mm_rule == 0, "Micrometer 的 http_server_requests_seconds 不改名（規則、RCA 看不到）",
          f"原名 {mm} 筆、label uri {mm_uri} 筆，在 {HIST} 底下 {mm_rule} 筆")

    print("== 3. 容器重啟、Pod 換掉：直方圖從 0 重新累加，換成增量後有沒有負值、對不上、掉在哪")
    for label, t, nxt in (("容器重啟（同一個 Pod）", t_crash, t_del), ("Pod 刪掉重建（新 Pod 名稱）", t_del, t_end)):
        docs = metric_docs(es, t - timedelta(seconds=90), nxt - timedelta(seconds=30), "/edge/partial/{id}")
        if not docs:
            check(False, label, "這段時間沒有 metrics")
            continue
        counts = [doc_count(d) for d in docs]
        lo = docs[0]["start_timestamp"]
        hi = docs[-1]["@timestamp"]
        tr = [x for x in traces(es, datetime.fromtimestamp(lo / 1000 - 10, timezone.utc), datetime.fromtimestamp(hi / 1000, timezone.utc),
                                "GET /edge/partial/{id}") if lo < x["end"] <= hi]
        # 每個 Pod 的抓取（start_timestamp, @timestamp］；每次抓取跟同 Pod、同區間結束的 trace 比
        by_pod: dict[str, list[tuple[int, int, int]]] = {}
        for d, c in zip(docs, counts):
            by_pod.setdefault(d["resource"]["attributes"]["k8s.pod.name"], []).append((d["start_timestamp"], d["@timestamp"], c))
        diffs = [c - sum(1 for x in tr if x["pod"] == pod and a_ < x["end"] <= b_) for pod, ivs in by_pod.items() for a_, b_, c in ivs]
        # 允許的缺口（每個 Pod 自己的）：第一次被保留的抓取之前（開頭）、最後一次抓取之後（尾巴）、同一個 Pod 跨過重啟的那一段；其他缺口不該有。
        # 新 Pod 最前面約 1 秒的 trace 沒有 Pod 名（k8s_attributes 還不認得它的 IP），算進開頭：第一個有 Pod 名的 trace 前 5 秒內
        allowed, holes, first = [], 0, []
        for pod, ivs in by_pod.items():
            ends = [x["end"] for x in tr if x["pod"] == pod]
            head = min(ends, default=ivs[0][0])
            allowed += [(pod, head - 1, ivs[0][0]), (None, head - 5000, ivs[0][0]), (pod, ivs[-1][1], max(ends, default=ivs[-1][1]))]
            if ends and ivs[0][0] - min(ends) > 1000:
                first.append(f"{pod[-5:]} 開始後 {(ivs[0][0] - min(ends)) / 1000:.0f} 秒")
            for (_, b1, _), (a2, _, _) in zip(ivs, ivs[1:]):
                if a2 - b1 > 1000:
                    if b1 < ms(t) <= a2:
                        allowed.append((pod, b1, a2))
                        first.append(f"{pod[-5:]} 重啟後 {(a2 - ms(t)) / 1000:.0f} 秒")
                    else:
                        holes += 1
        gap = [x for x in tr if not any(a_ < x["end"] <= b_ for a_, b_, _ in by_pod.get(x["pod"], []))]
        bad = [x for x in gap if not any(p_ == x["pod"] and a_ < x["end"] <= b_ for p_, a_, b_ in allowed)]
        check(min(counts) >= 0 and max(map(abs, diffs)) <= 2 and holes == 0 and not bad,
              f"{label}：沒有負值；每次抓取跟同 Pod 的 trace 差 ≤ 2；只掉重啟前後那段",
              f"{len(docs)} 筆 metrics（每筆 {min(counts)}～{max(counts)}）、跟 trace 差 {min(diffs)}～{max(diffs)}；"
              f"trace {len(tr)} 個、metrics 加總 {sum(counts)}，少 {len(tr) - sum(counts)}；"
              f"沒被抓到的 {len(gap)} 個裡不在開頭／尾巴的 {len(bad)} 個（沒有 Pod 名 {sum(1 for x in gap if not x['pod'])}）；"
              f"其他缺口 {holes} 段；Pod {len(by_pod)} 個；第一次被保留的抓取：{'、'.join(first) or '無'}")

    print("== 4. 30 秒抓一次 vs trace 的時間窗；主機時鐘差")
    docs = metric_docs(es, t_norm, t_crash, "/edge/partial/{id}")
    gaps = sorted((d["@timestamp"] - d["start_timestamp"]) / 1000 for d in docs)
    check(gaps and 25 <= gaps[len(gaps) // 2] <= 35, "每筆 metrics 涵蓋（start_timestamp, @timestamp］約 30 秒",
          f"{len(docs)} 筆，中位數 {gaps[len(gaps) // 2]:.1f} 秒、最短 {gaps[0]:.1f}、最長 {gaps[-1]:.1f}")
    scrapes = [(d["start_timestamp"], d["@timestamp"]) for d in docs]
    slow = [x for x in traces(es, t_inc, t_heal, "GET /edge/partial/{id}") if x["slow"]]

    def mismatch(skew_s: float) -> tuple[int, int]:
        """慢的請求：metrics 記在哪個 5 分鐘（請求結束後下一次抓取的時間）vs trace 落在哪個 5 分鐘（開始時間＋時鐘差）。"""
        n = bad = 0
        for x in slow:
            sc = next((b for a_, b in scrapes if a_ < x["end"] <= b), None)
            if sc is None:
                continue
            n += 1
            bad += bucket(sc) != bucket(x["start"] + skew_s * 1000)
        return bad, n

    bad, n = mismatch(0)
    check(n > 0 and bad / n < 0.15, "慢的請求在 metrics、trace 落在同一個 5 分鐘", f"{n} 個慢請求，{bad} 個（{bad / max(n, 1):.1%}）落在不同的 5 分鐘")
    for sk in (1, 3, 5, -5):
        b, n = mismatch(sk)
        print(f"        trace 時鐘{'快' if sk > 0 else '慢'} {abs(sk)} 秒：{b}/{n}（{b / max(n, 1):.1%}）落在不同的 5 分鐘")
    first_slow = min(x["start"] for x in slow) if slow else None
    first_doc = next((d["@timestamp"] for d in docs if d["@timestamp"] >= ms(t_inc) and max_s(d) > 1), None)
    lag = (first_doc - first_slow) / 1000 if first_slow and first_doc else None
    check(lag is not None and 0 <= lag <= 35, "事故開始：metrics 比 trace 晚多久看到", f"{lag:.1f} 秒" if lag is not None else "沒量到")

    print(f"== 5. 只有 10% 的請求慢 2 秒：挑出來的 trace 是不是那些慢的（時間窗 {b0:%H:%M}～{b0 + W:%H:%M}Z）")
    truth = {x["id"] for x in traces(es, b0, b0 + W, "GET /edge/partial/{id}") if x["slow"]}
    for label, src in (("測試環境（trace 全留）", es), ("正式（慢的全留、其他隨機 5%）", sampled)):
        ids = E.sample_trace_ids(src, TIX, SVC, "GET /edge/partial/{id}", b0, b0 + W, 200, slowest=True)
        hit = len(truth & set(ids))
        print(f"        {label}：樣本 {len(ids)} 個，其中慢的 {hit}（{hit / max(len(ids), 1):.0%}）；慢的共 {len(truth)} 個，撈到 {hit / max(len(truth), 1):.0%}")
        if src is sampled:
            check(len(truth) > 0 and hit == len(truth), "慢的請求全部在樣本裡", f"{hit}/{len(truth)}")
            check(hit / max(len(ids), 1) >= 0.5, "樣本裡多數是慢的（正式取樣）", f"{hit}/{len(ids)}（{hit / max(len(ids), 1):.0%}）")

    print("== 6. 不到 1 秒的變慢（200ms → 800ms）：只剩隨機 5% 的 trace")
    m, mp95 = metric_count(es, b0, b0 + W, "/edge/sub1s/{id}")
    full = E.entry_stats(es, TIX, SVC, "GET /edge/sub1s/{id}", b0, b0 + W)
    kept = E.entry_stats(sampled, TIX, SVC, "GET /edge/sub1s/{id}", b0, b0 + W)
    check(m > 0 and 0.02 <= kept["requests"] / m <= 0.09, "正式取樣下撈得到約 5%",
          f"metrics {m} 個請求（p95 {mp95 * 1000:.0f}ms）；trace 全留 {full['requests']} 個、正式取樣剩 {kept['requests']} 個"
          f"（{kept['requests'] / max(m, 1):.1%}），p95 {kept['p95_ms']:.0f}ms")

    print("== 7. 應用自設的 service.name（mt-edge-otel）被 Pod label 蓋掉：metrics、trace 同一個名字")
    docs = metric_docs(es, b0, b0 + W, "/mismatch/{id}")
    k = keys_of(docs[0]) if docs else {}
    pod = docs[0]["resource"]["attributes"]["k8s.pod.name"] if docs else ""
    got = E.entry_stats(es, TIX, k.get("resource.attributes.service.name", SVC), _route_of(k), b0, b0 + W)["requests"] if docs else -1
    real = es.search(TIX, {"size": 0, "track_total_hits": True, "query": {"bool": {"filter": [
        time_range(b0, b0 + W), {"term": {"kind": "Server"}}, {"term": {"name": "GET /mismatch/{id}"}},
        {"term": {"resource.attributes.k8s.pod.name": pod}}]}}})["hits"]["total"]["value"]
    check(got > 0 and got == real, "用 metrics 的服務名稱撈得到這個 Pod 這條路由的全部 trace",
          f"metrics service.name={k.get('resource.attributes.service.name')!r}、撈到 {got} 筆；Pod 的 GET /mismatch/{{id}} 共 {real} 筆")
    r = es.search(TIX, {"size": 0, "query": {"bool": {"filter": [time_range(b0, b0 + W), {"term": {"kind": "Server"}},
                                                                  {"term": {"resource.attributes.k8s.pod.name": pod}}]}},
                        "aggs": {"s": {"terms": {"field": "resource.attributes.service.name"}}}})
    names = {b["key"]: b["doc_count"] for b in r["aggregations"]["s"]["buckets"]}
    check(list(names) == [SVC], "用 metrics 的 Pod 名稱反查 trace 的 service.name：只有一個、跟 metrics 一樣", f"Pod {pod} 的 trace：{names}")

    print(f"\n{'全部 PASS' if not fails else f'{fails} 項 FAIL'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

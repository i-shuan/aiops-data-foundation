"""trace 每一段的異常、資料品質檢查：邊緣情境（docs/pdca/phase2-anomaly/2026-10-trace-quality-verify.md）。

需要自己的 ES（跑過 elasticsearch/code/scripts/elasticsearch-setup.sh）。不要對共用 cluster 跑：會刪 tq- 開頭的服務的資料。
  ES_URL=http://127.0.0.1:31504 python -X utf8 -m tests.trace_quality_edge_e2e      （在 engine/ 底下）
可重跑：每次先刪 tq- 開頭服務的 trace、log、metrics、統計、baseline、異常，時間以「現在」為準重新產生。每項印 PASS／FAIL。

A. trace 每一段（trace-collect、trace-train、trace-detect 實跑，設定 tests/trace_edge.yaml：1 分鐘一點）
   模擬尾端取樣（collector/code/sampler.yaml）：每分鐘 400 個請求（4% 錯、3% 慢 1.5～3 秒），
   100% 收、不取樣（以前這裡模擬尾端取樣，已拆掉）；
   寫進 traces-generic.otel-<group>，ES 的 traces-otel pipeline 把不是 random 的轉到 traces-problem.otel-*。
   正常 40 分鐘學 baseline，之後每個情境 3 分鐘。
B. 資料品質（quality.yaml 的檢查原樣用；window 改 30m、ref_offset 改 1h：metrics 是 TSDS，只收最近 2 小時）
"""
from __future__ import annotations

import copy
import io
import os
import random
import sys
import tempfile
from contextlib import redirect_stdout

import pandas as pd
import yaml

from aiops.interface.cli.detect import main as cli
from aiops.service.detect.quality import quality_hits
from aiops.core.connection import ES

CFG = "tests/trace_edge.yaml"
QUALITY = "configs/baseline/quality.yaml"
MES, WIP = "tq-mes-api", "tq-wip-worker"
API = "GET /lots/{id}"
SEG_DB = f"{WIP} › SELECT LOT_HISTORY → oracle"
SEG_SMS = f"{MES} › POST → sms-gateway"
SEG_REDIS = f"{MES} › HGET → redis"
PER_MIN = 400          # 每分鐘的請求（尾端取樣前）
RARE_PER_MIN = 40      # 少人用的 API（GET /rare）

results: list[tuple[str, str, bool, str]] = []


def check(group: str, name: str, ok: bool, detail: str = ""):
    results.append((group, name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"：{detail}" if detail else ""))


# ── 寫入 ──

def bulk(es: ES, docs: list[tuple[str, dict]], chunk: int = 4000) -> list[dict]:
    """docs：(index, 文件)，用 create（資料流只收 create）。回傳每筆的結果。"""
    import json
    items = []
    for i in range(0, len(docs), chunk):
        lines = []
        for index, d in docs[i:i + chunk]:
            lines += [json.dumps({"create": {"_index": index}}), json.dumps(d, default=str)]
        r = es.s.post(f"{es.url}/_bulk", data="\n".join(lines) + "\n",
                      headers={"Content-Type": "application/x-ndjson"}, timeout=600)
        r.raise_for_status()
        body = r.json()
        items += [it["create"] for it in body["items"]]
        bad = [it["create"] for it in body["items"] if it["create"].get("error")
               and it["create"]["error"].get("type") != "version_conflict_engine_exception"]   # 重送的
        if bad:
            raise RuntimeError(f"bulk 有 {len(bad)} 筆被拒：{bad[:2]}")
    return items


def refresh(es: ES, pattern: str):
    es.s.post(f"{es.url}/{pattern}/_refresh?ignore_unavailable=true&allow_no_indices=true&expand_wildcards=all", timeout=120)


def cleanup(es: ES):
    q = lambda f: {"query": {"prefix": {f: "tq-"}}}
    for index, field in [("traces-*.otel-*", "resource.attributes.service.name"),
                         ("metrics-*.otel-*", "resource.attributes.service.name"),
                         ("metrics-*.otel-*", "resource.attributes.k8s.namespace.name"),
                         ("k8s-*", "kubernetes.namespace"),
                         ("aiops-trace-segments", "entry.service"),
                         ("aiops-baseline", "keys.entry.service"),
                         ("aiops-anomalies", "keys.entry.service"),
                         ("aiops-anomalies", "keys.service"),
                         ("aiops-anomalies", "keys.namespace")]:
        es.s.post(f"{es.url}/{index}/_delete_by_query?conflicts=proceed&refresh=true&ignore_unavailable=true"
                  f"&allow_no_indices=true&expand_wildcards=all", json=q(field), timeout=600)
    # B6b 放的「沒有 K8s 中繼資料」的 log（沒有 namespace 可以篩，用 pod、namespace 都沒有＋訊息 ok 找）
    es.s.post(f"{es.url}/k8s-*/_delete_by_query?conflicts=proceed&refresh=true&ignore_unavailable=true&allow_no_indices=true",
              json={"query": {"bool": {"must": [{"match": {"message": "ok"}}],
                                       "must_not": [{"exists": {"field": "kubernetes.namespace"}},
                                                    {"exists": {"field": "kubernetes.pod.name"}}]}}}, timeout=600)
    # failure store 裡的舊文件（上一輪故意放的壞資料）
    for fs in ["k8s-*::failures", "traces-*.otel-*::failures"]:
        es.s.post(f"{es.url}/{fs}/_delete_by_query?conflicts=proceed&refresh=true&ignore_unavailable=true"
                  f"&allow_no_indices=true", json={"query": {"match_all": {}}}, timeout=600)


# ── A. trace ──

def hexid(rng: random.Random, n: int) -> str:
    return f"{rng.getrandbits(n * 4):0{n}x}"


def span_doc(tid, sid, parent, svc, ns, name, kind, start_ms, dur_ms, *, error=False, attrs=None, policy=None,
             group="it", status_code=None):
    a = dict(attrs or {})
    if status_code:
        a["http.response.status_code"] = status_code
    d = {"@timestamp": pd.Timestamp(start_ms, unit="ms", tz="UTC").isoformat(timespec="milliseconds"),
         "data_stream": {"type": "traces", "dataset": "generic.otel", "namespace": group},
         "trace_id": tid, "span_id": sid, "name": name, "kind": kind, "duration": int(dur_ms * 1e6),
         "resource": {"attributes": {"service.name": svc, "k8s.namespace.name": ns}},
         "attributes": a}
    if parent:
        d["parent_span_id"] = parent
    if error:
        d["status"] = {"code": "Error"}
    if policy:
        d["scope"] = {"name": "tq", "attributes": {"tailsampling.policy": policy}}
    return d


def lots_request(rng: random.Random, t0: float, mode: str) -> tuple[list[dict], dict]:
    """mes-api 收到 GET /lots/{id}：5% 快取沒命中查 Oracle；呼叫 wip-worker（查 LOT_HISTORY）；送簡訊。
    回傳 (span 文件的內容（還沒決定 policy）, 這個請求的真實值：wip 那段錯了沒、簡訊花多久)。"""
    u = rng.random()
    kind = "error" if u < 0.04 else "slow" if u < 0.07 else "normal"
    if mode == "propagated":
        kind = "error"
    q = 60 if mode in ("n_plus_1", "n_plus_1_semconv") else 1
    db_ms = 80.0 if mode == "db_slow" else 2.0
    sms = rng.uniform(1500, 3000) if kind == "slow" else rng.uniform(15, 25)
    if mode == "partial_slow" and rng.random() < 0.10:
        sms += 2000
    db_exc = None          # wip 查 DB 那段：None、"unset"（例外、沒標錯誤）、"error"（例外＋標錯誤）
    wip_fail = mes_fail = False
    if kind == "error":
        db_exc, wip_fail, mes_fail = "error", True, True
    elif mode == "swallow_unset" or (mode == "chronic_swallow" and rng.random() < 0.02):
        db_exc = "unset"
    elif mode == "swallow_java":
        db_exc = "error"   # Java agent：JDBC 丟例外就標成錯誤；程式 catch 掉、wip-worker 回 200

    tid = hexid(rng, 32)
    ids = iter(hexid(rng, 16) for _ in range(10_000))
    spans = []
    root = next(ids)
    t = t0 + 2
    if mode != "gone_seg_db" and rng.random() < 0.05:
        spans.append(dict(sid=next(ids), parent=root, svc=MES, ns="tq-mes", name="SELECT LOT", kind="Client",
                          start=t, dur=5, attrs={"db.system": "oracle"}))
        t += 5
    if mode == "new_seg":
        spans.append(dict(sid=next(ids), parent=root, svc=MES, ns="tq-mes", name="HGET", kind="Client",
                          start=t, dur=1, attrs={"db.system": "redis"}))
        t += 1
    w = 2 + q * db_ms
    c, s = next(ids), next(ids)
    spans.append(dict(sid=c, parent=root, svc=MES, ns="tq-mes", name="GET", kind="Client", start=t, dur=w + 2,
                      attrs={"peer.service": WIP}, error=mes_fail))
    spans.append(dict(sid=s, parent=c, svc=WIP, ns="tq-wip", group="imc", name="GET /wip", kind="Server", start=t + 1,
                      dur=w, error=wip_fail, status=500 if wip_fail else 200))
    for j in range(q):
        db_attrs = {"db.system.name": "oracle"} if mode == "n_plus_1_semconv" else {"db.system": "oracle"}
        err = False
        if j == 0 and db_exc:
            db_attrs["exception.type"] = "java.sql.SQLTimeoutException"
            err = db_exc == "error"
        spans.append(dict(sid=next(ids), parent=s, svc=WIP, ns="tq-wip", group="imc", name="SELECT LOT_HISTORY",
                          kind="Client", start=t + 2 + j * db_ms, dur=db_ms, attrs=db_attrs, error=err))
    t += w + 2
    if mode != "gone_seg":
        spans.append(dict(sid=next(ids), parent=root, svc=MES, ns="tq-mes", name="POST", kind="Client", start=t,
                          dur=sms, attrs={"peer.service": "sms-gateway"}))
        t += sms
    spans.insert(0, dict(sid=root, parent=None, svc=MES, ns="tq-mes", name=API, kind="Server", start=t0, dur=t - t0 + 1,
                         error=mes_fail, status=500 if mes_fail else 200))
    truth = {"db_error": db_exc == "error", "sms_ms": 0.0 if mode == "gone_seg" else sms, "dur": t - t0 + 1}
    return [dict(sp, tid=tid) for sp in spans], truth


def rare_request(rng: random.Random, t0: float) -> tuple[list[dict], dict]:
    tid, root, c = hexid(rng, 32), hexid(rng, 16), hexid(rng, 16)
    sms = rng.uniform(15, 25)
    spans = [dict(tid=tid, sid=root, parent=None, svc=MES, ns="tq-mes", name="GET /rare", kind="Server", start=t0,
                  dur=sms + 3, status=200),
             dict(tid=tid, sid=c, parent=root, svc=MES, ns="tq-mes", name="POST", kind="Client", start=t0 + 2,
                  dur=sms, attrs={"peer.service": "sms-gateway"})]
    return spans, {"dur": sms + 3}


def tail_sample(rng: random.Random, spans: list[dict], dur: float) -> str | None:
    """以前這裡模擬 trace-sampler 的決定；現在 100% 收、不取樣，全部留（policy 一律 random，sample_policy 的過濾照舊通過）。"""
    return "random"


def to_docs(spans: list[dict], policy: str) -> list[tuple[str, dict]]:
    out = []
    for s in spans:
        g = s.get("group", "it")
        out.append((f"traces-generic.otel-{g}",
                    span_doc(s["tid"], s["sid"], s["parent"], s["svc"], s["ns"], s["name"], s["kind"], s["start"],
                             s["dur"], error=s.get("error", False), attrs=s.get("attrs"), policy=policy, group=g,
                             status_code=s.get("status"))))
    return out


def gen_traffic(rng: random.Random, start: pd.Timestamp, minutes: int, mode: str, truth: list | None = None):
    docs = []
    for m in range(minutes):
        base = (start + pd.Timedelta(minutes=m)).value / 1e6
        for _ in range(PER_MIN):
            spans, tr = lots_request(rng, base + rng.uniform(0, 55_000), mode)
            if truth is not None:
                truth.append(tr)
            p = tail_sample(rng, spans, tr["dur"])
            if p:
                docs += to_docs(spans, p)
        for _ in range(RARE_PER_MIN):
            spans, tr = rare_request(rng, base + rng.uniform(0, 55_000))
            p = tail_sample(rng, spans, tr["dur"])
            if p:
                docs += to_docs(spans, p)
    return docs


SCENARIOS = [   # (模式, 說明)
    ("normal", "正常（4% 錯、3% 慢的長尾）"),
    ("db_slow", "每段延遲：wip-worker 查 DB 每次 2 → 80 ms"),
    ("n_plus_1", "N+1：wip-worker 每個請求查 60 次"),
    ("n_plus_1_semconv", "N+1，DB span 只有新版欄位 db.system.name"),
    ("swallow_unset", "例外吞噬：DB 例外、status 沒設、回 200"),
    ("swallow_java", "例外吞噬（Java agent 樣子）：DB span 標錯誤＋例外，wip-worker 回 200"),
    ("propagated", "例外沒被吞：DB 錯誤一路傳到入口 500"),
    ("new_seg", "新出現的段：mes-api 每個請求多查 redis"),
    ("gone_seg", "消失的段：mes-api 不再送簡訊"),
    ("partial_slow", "只有部分請求變慢：10% 的請求簡訊多 2 秒"),
    ("chronic_swallow", "長期 2% 吞例外（低於規則門檻 5%）"),
]


def run_cli(*argv) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli(list(argv))
    if rc:
        raise RuntimeError(f"detector {' '.join(argv)} 回 {rc}：{buf.getvalue()[-500:]}")
    return buf.getvalue()


def anomalies(es: ES, a: pd.Timestamp, b: pd.Timestamp) -> set[tuple]:
    refresh(es, "aiops-anomalies")
    body = {"size": 2000, "query": {"bool": {"filter": [
        {"prefix": {"series": "trace."}}, {"range": {"@timestamp": {"gte": a.isoformat(), "lt": b.isoformat()}}},
        {"terms": {"keys.entry.service": [MES, WIP]}}]}}}
    out = set()
    for h in es.search("aiops-anomalies", body)["hits"]["hits"]:
        s = h["_source"]
        out.add((s["series"].replace("trace.segment.", ""), s["keys"]["entry.service"], s["keys"]["segment"],
                 s.get("status") or "rule"))
    return out


def has(found: set, metric: str, svc: str, seg: str) -> bool:
    return any(f[0] == metric and f[1] == svc and f[2] == seg for f in found)


def fmt(found: set) -> str:
    return "；".join(f"{m}|{s}|{g}|{st}" for m, s, g, st in sorted(found)) or "（無）"


def part_a(es: ES, now0: pd.Timestamp):
    print("\n== A. trace 每一段")
    rng = random.Random(20261028)
    t0 = now0 - pd.Timedelta(minutes=100)
    hist_end = t0 + pd.Timedelta(minutes=40)
    docs = gen_traffic(rng, t0, 40, "normal")
    windows = []
    for i, (mode, desc) in enumerate(SCENARIOS):
        a = hist_end + pd.Timedelta(minutes=1 + i * 5)
        windows.append((mode, desc, a, a + pd.Timedelta(minutes=3)))
        docs += gen_traffic(rng, a, 3, mode)
    print(f"  寫入 {len(docs)} 個 span（尾端取樣後）")
    bulk(es, docs)
    refresh(es, "traces-*.otel-*")


    # 學
    run_cli("trace-collect", "--config", CFG, "--start", t0.isoformat(), "--end", hist_end.isoformat())
    out = run_cli("trace-train", "--config", CFG, "--history", "40min", "--end", hist_end.isoformat())
    print("  " + out.strip().replace("\n", "\n  "))

    # 每個情境
    want = {   # 模式：(要抓到的 [(數字, 服務, 段)], 不該出現的 [(數字, 服務, 段)])
        "normal": ([], None),
        "db_slow": ([("time_ms", WIP, SEG_DB)], []),
        "n_plus_1": ([("calls", WIP, SEG_DB), ("repeated", WIP, SEG_DB)], [("repeated", MES, "*")]),
        "n_plus_1_semconv": ([("repeated", WIP, SEG_DB)], []),
        "swallow_unset": ([("swallowed", WIP, SEG_DB), ("swallowed_rate", WIP, SEG_DB)], []),
        "swallow_java": ([("swallowed", WIP, SEG_DB)], []),
        "propagated": ([("error_rate", WIP, SEG_DB)], [("swallowed", WIP, SEG_DB), ("swallowed_rate", WIP, SEG_DB)]),
        "new_seg": ([("new", MES, SEG_REDIS)], []),
        "gone_seg": ([("calls", MES, SEG_SMS)], []),
        "partial_slow": ([("time_ms", MES, SEG_SMS)], []),
        "chronic_swallow": ([], [("swallowed", WIP, SEG_DB), ("swallowed_rate", WIP, SEG_DB)]),
    }
    for mode, desc, a, b in windows:
        run_cli("trace-detect", "--config", CFG, "--window", "3min", "--at", b.isoformat())
        found = anomalies(es, a, b)
        hits, forbid = want[mode]
        if forbid is None:   # 正常：什麼都不該有
            check("A", desc + "：不報", not found, fmt(found))
            continue
        for m, svc, seg in hits:
            check("A", f"{desc}：報 {m} {seg}", has(found, m, svc, seg), fmt(found))
        for m, svc, seg in forbid:
            bad = {f for f in found if f[0] == m and f[1] == svc and (seg == "*" or f[2] == seg)}
            check("A", f"{desc}：不報 {m} {svc if seg == '*' else seg}", not bad, fmt(bad))


# ── B. 資料品質 ──

def iso(ts: pd.Timestamp) -> str:
    return ts.isoformat(timespec="milliseconds")


def spread(start: pd.Timestamp, end: pd.Timestamp, n: int) -> list[pd.Timestamp]:
    step = (end - start) / max(n, 1)
    return [start + step * (i + 0.5) for i in range(n)]


def metric(ts, svc, ns, route, group="it", pod="p-1"):
    res = {"k8s.namespace.name": ns, "k8s.pod.name": pod}
    if svc:
        res["service.name"] = svc
    return (f"metrics-generic.otel-{group}",
            {"@timestamp": iso(ts), "data_stream": {"type": "metrics", "dataset": "generic.otel", "namespace": group},
             "resource": {"attributes": res}, "attributes": {"http.route": route, "http.request.method": "GET"},
             "metrics": {"http.server.request.duration": 0.05}})


def log(ts, svc, ns, group="it", trace=False, k8s=True):
    """Fluent Bit 送進 k8s-<group>-<cluster> 的樣子（k8s-logs pipeline 依這個補服務、Pod、trace.id）。
    svc=None：Pod 沒有 app.kubernetes.io/name；k8s=False：kubernetes filter 沒補到中繼資料。"""
    raw = {"message": "ok", "level": "info"}
    if trace:
        raw["trace_id"] = "0af7651916cd43dd8448eb211c80319c"
    import json
    d = {"@timestamp": iso(ts), "log": json.dumps(raw)}
    if k8s:
        d["kubernetes"] = {"namespace_name": ns, "pod_name": f"{ns}-pod", "container_name": "app",
                           "labels": {"app.kubernetes.io/name": svc} if svc else {}}
    return (f"k8s-{group}-local", d)


def entry_span(ts, svc, name, group="it"):
    return (f"traces-generic.otel-{group}",
            span_doc(f"{random.getrandbits(128):032x}", f"{random.getrandbits(64):016x}", None, svc, "tq-q", name,
                     "Server", ts.value / 1e6, 5, group=group))


def quality_data(now: pd.Timestamp) -> list[tuple[str, dict]]:
    ref = (now - pd.Timedelta(minutes=90), now - pd.Timedelta(minutes=60))
    cur = (now - pd.Timedelta(minutes=30), now - pd.Timedelta(seconds=30))
    both = [ref, cur]
    d: list = []
    # B1 全部沒參數化：昨天 3 條樣板，今天多 120 條 /lots/<數字>
    for w in both:
        for r in ["/lots/{id}", "/lots", "/health"]:
            d += [metric(t, "tq-q-lots", "tq-q-ns-lots", r) for t in spread(*w, 10)]
    d += [metric(t, "tq-q-lots", "tq-q-ns-lots", f"/lots/{100000 + i}") for i, t in enumerate(spread(*cur, 120))]
    # B2 對照：3 條樣板路由
    for w in both:
        for r in ["/pay/{id}", "/pay", "/health"]:
            d += [metric(t, "tq-q-pay", "tq-q-ns-pay", r) for t in spread(*w, 10)]
    # B3 大服務只有一部分沒參數化：40 條樣板（量大）＋45 條 /big/<數字>（各 1 筆）
    for w in both:
        for i in range(40):
            d += [metric(t, "tq-q-big", "tq-q-ns-big", f"/big/op{i}/{{id}}") for t in spread(*w, 30)]
    d += [metric(t, "tq-q-big", "tq-q-ns-big", f"/big/{200000 + i}") for i, t in enumerate(spread(*cur, 45))]
    # B4 路由本來就多（60 條樣板），兩段一樣
    for w in both:
        for i in range(60):
            d += [metric(t, "tq-q-wide", "tq-q-ns-wide", f"/wide/op{i}/{{id}}") for t in spread(*w, 5)]
    # B5 trace 的 API 名稱沒參數化：昨天 2 個，今天多 120 個 GET /orders/<數字>
    for w in both:
        for n in ["GET /orders/{id}", "POST /orders"]:
            d += [entry_span(t, "tq-q-tr", n) for t in spread(*w, 20)]
    d += [entry_span(t, "tq-q-tr", f"GET /orders/{300000 + i}") for i, t in enumerate(spread(*cur, 120))]
    # B6a log 三成的 Pod 沒有 app.kubernetes.io/name（pipeline 退回 container 名稱 app）
    d += [log(t, None if i % 10 < 3 else "tq-q-mes", "tq-q-ns-mes") for i, t in enumerate(spread(*cur, 400))]
    # B6b 另外 200 行 Fluent Bit 沒補到 K8s 中繼資料（沒有 namespace、Pod、服務）
    d += [log(t, None, None, k8s=False) for t in spread(*cur, 200)]
    # B7 metrics 沒有 service.name
    for w in both:
        d += [metric(t, None, "tq-q-ns-nosvc", "/x") for t in spread(*w, 50)]
    # B8 namespace 沒標 dept → unassigned 變多（log：2% → 約 17%；metrics：約 1% → 約 25%）
    for w in both:
        d += [log(t, "tq-q-logbase", "tq-q-ns-base") for t in spread(*w, 1000)]
    d += [log(t, "tq-q-nodept", "tq-q-ns-nodept", group="unassigned") for t in spread(*ref, 20)]
    d += [log(t, "tq-q-nodept", "tq-q-ns-nodept", group="unassigned") for t in spread(*cur, 300)]
    d += [metric(t, "tq-q-nodept", "tq-q-ns-nodept", "/n", group="unassigned") for t in spread(*ref, 20)]
    d += [metric(t, "tq-q-nodept", "tq-q-ns-nodept", "/n", group="unassigned") for t in spread(*cur, 900)]
    # B9 資料停了：最近沒有（log 20 分鐘、metrics 10 分鐘、trace 20 分鐘）
    d += [log(t, "tq-q-stop", "tq-q-ns-stop") for t in spread(now - pd.Timedelta(minutes=60), now - pd.Timedelta(minutes=20), 100)]
    d += [metric(t, "tq-q-mstop", "tq-q-ns-mstop", "/m") for t in spread(now - pd.Timedelta(minutes=60), now - pd.Timedelta(minutes=10), 60)]
    d += [entry_span(t, "tq-q-tstop", "GET /t") for t in spread(now - pd.Timedelta(minutes=60), now - pd.Timedelta(minutes=20), 60)]
    # B12 帶 trace.id 的 log 變少：昨天 90%、今天 20%
    d += [log(t, "tq-q-tid", "tq-q-ns-tid", trace=i % 10 < 9) for i, t in enumerate(spread(*ref, 300))]
    d += [log(t, "tq-q-tid", "tq-q-ns-tid", trace=i % 10 < 2) for i, t in enumerate(spread(*cur, 300))]
    return d


def q_hits(es, cfg, now, name, ref_offset=None, **override) -> list[dict]:
    c = copy.deepcopy(next(x for x in cfg["checks"] if x["name"] == name))
    c.update(override)
    window = pd.Timedelta(c.get("window", cfg["window"]))
    return quality_hits(es, c, now, window, pd.Timedelta(ref_offset or cfg["ref_offset"]))


def keys_of(hits: list[dict]) -> list[str]:
    return [",".join(str(v) for v in h["key"].values() if v is not None) + (f"[{h['field']}]" if "field" in h else "")
            for h in hits]


def part_b(es: ES, now: pd.Timestamp):
    print("\n== B. 資料品質")
    items = bulk(es, quality_data(now))
    # B10 failure store：時間壞掉的 log、span（ES 回 201，進 <資料流>::failures）。
    # 型別不合的 span（duration 是字串）不會被拒：traces 是 logsdb，ignore_malformed，存進去、那個欄位丟掉
    bad_span = span_doc("f" * 32, "f" * 16, None, "tq-q-bad", "tq-q", "GET /bad", "Server", now.value / 1e6, 1)
    bad = bulk(es, [("k8s-it-local", {"@timestamp": "not-a-date", "log": "bad",
                                      "kubernetes": {"namespace_name": "tq-q-ns-bad", "pod_name": "x"}}),
                    ("traces-generic.otel-it", dict(bad_span, **{"@timestamp": "not-a-date"})),
                    ("traces-generic.otel-it", dict(bad_span, span_id="e" * 16, duration="not-a-number"))])
    print(f"  寫入 {len(items)} 筆；壞資料（log 時間壞、span 時間壞、span duration 是字串）進 failure store："
          f"{[b.get('failure_store', 'not_used') for b in bad]}")
    refresh(es, "k8s-*,metrics-*.otel-*,traces-*.otel-*")
    refresh(es, "k8s-*::failures,traces-*.otel-*::failures")

    with open(QUALITY, encoding="utf-8") as f:
        real = yaml.safe_load(f)
    cfg = dict(real, window="30m", ref_offset="1h")
    names = {c["name"] for c in cfg["checks"]}

    def run(name, **kw):
        if name not in names:
            return None
        return keys_of(q_hits(es, cfg, now, name, **kw))

    def expect(label, name, want: list[str], dont: list[str], **kw):
        got = run(name, **kw)
        if got is None:
            check("B", f"{label}（{name}）", False, "quality.yaml 沒有這個檢查")
            return
        got_tq = [g for g in got if "tq-" in g or g == "" or g.startswith("[")]   # 沒有分組的、沒有 namespace 的也列
        ok = all(any(w == g for g in got) for w in want) and not any(any(x == g for g in got) for x in dont)
        check("B", f"{label}（{name}）", ok, f"報了 {got_tq or '（無）'}")

    expect("B1 路由全部沒參數化 → 報；B2 樣板路由、B4 路由本來就多 → 不報", "quality.route_cardinality",
           ["tq-q-lots"], ["tq-q-pay", "tq-q-wide"])
    expect("B3 大服務一部分沒參數化（40 樣板＋45 像 ID，53%）→ 報", "quality.route_cardinality", ["tq-q-big"], [])
    expect("B5 trace 的 API 名稱沒參數化 → 報", "quality.trace_name_cardinality", ["tq-q-tr"], [])
    expect("B6a log 三成的 Pod 沒有服務 label → 報（服務身分對不上 metrics、trace）", "quality.log_missing_identity",
           ["tq-q-ns-mes[resource.attributes.service.name]"], [])
    expect("B6b log 沒補到 K8s 中繼資料 → 報", "quality.log_missing_identity",
           ["[resource.attributes.service.name]"], [])
    expect("B7 metrics 缺 service.name → 報", "quality.metrics_missing_service",
           ["tq-q-ns-nosvc[resource.attributes.service.name]"], [])
    expect("B8 namespace 沒標 dept：unassigned 的 log 2% → 17% → 報", "quality.log_unassigned_share", [""], [])
    expect("B8 namespace 沒標 dept：unassigned 的 metrics 1% → 25% → 報", "quality.metrics_unassigned_share", [""], [])
    expect("B9 log 停了 20 分鐘 → 報；有資料的不報", "quality.log_stopped", ["tq-q-stop"], ["tq-q-logbase", "tq-q-mes"])
    expect("B9 metrics 停了 10 分鐘 → 報；有資料的不報", "quality.metrics_stopped", ["tq-q-mstop"],
           ["tq-q-pay", "tq-q-lots", "tq-q-big"])
    expect("B9 trace 停了 20 分鐘 → 報；有資料的不報", "quality.trace_stopped", ["tq-q-tstop"], ["tq-q-tr"])
    # failure store 的 @timestamp 是被拒的時間（寫入當下），不是資料的時間：用現在當 now
    late = pd.Timestamp.now(tz="UTC").ceil("min")
    got = keys_of(q_hits(es, cfg, late, "quality.ingest_rejected")) if "quality.ingest_rejected" in names else []
    check("B", "B10 failure store 有被拒的 log、span → 兩個資料流都報（quality.ingest_rejected）",
          any("k8s-it-local" in g for g in got) and any("traces-generic.otel-it" in g for g in got), f"報了 {got}")
    expect("B12 帶 trace.id 的 log 90% → 20% → 報", "quality.log_trace_id_drop",
           ["tq-q-tid[trace_id]"], [])

    # 第一天：ref 那段完全沒資料（ref_offset 30 天）
    first = dict(ref_offset="720h")
    expect("第一天：像 ID 的路由仍報（抽樣比例）", "quality.route_cardinality", ["tq-q-lots"], [], **first)
    expect("第一天：樣板路由 60 條、3 條不報（沒有比較對象）", "quality.route_cardinality", [], ["tq-q-wide", "tq-q-pay"],
           **first)
    expect("第一天：unassigned 只跟昨天比，不報", "quality.log_unassigned_share", [], [""], **first)
    expect("第一天：unassigned 設 max_share 0.1 → 報", "quality.log_unassigned_share", [""], [], max_share=0.1, **first)
    expect("第一天：trace.id 變少只跟昨天比，不報", "quality.log_trace_id_drop", [], ["tq-q-tid[trace_id]"], **first)
    expect("第一天：缺欄位（絕對門檻）照報", "quality.log_missing_identity",
           ["[resource.attributes.service.name]"], [], **first)
    expect("第一天：資料停了（不用昨天）照報", "quality.log_stopped", ["tq-q-stop"], [], **first)

    # CLI 整個跑一次：設定檔可以載入、每個檢查都跑得完、結果寫進 aiops-anomalies
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
        path = f.name
    try:
        out = run_cli("quality", "--config", path, "--at", now.isoformat())
    finally:
        os.unlink(path)
    refresh(es, "aiops-anomalies")
    n = es.search("aiops-anomalies", {"size": 0, "track_total_hits": True, "query": {"bool": {"filter": [
        {"term": {"level": "quality"}}, {"range": {"detected_at": {"gte": (now - pd.Timedelta(minutes=5)).isoformat()}}}]}}})
    n = n["hits"]["total"]["value"]
    check("B", "quality 指令整個跑完、寫進 aiops-anomalies", n > 0, out.strip().splitlines()[-1] + f"；寫入 {n} 筆")


def _retry():
    """本機 ES 很忙時，連線常被重設（WinError 10054）：每個請求（含 POST）都重試。
    重送不會多一份：trace 的 _id 由 pipeline 用 trace_id-span_id 設、aiops-* 都帶 _id，重複的 create 回 409（bulk() 略過）。"""
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    import aiops.core.connection as conn
    init = conn.ES.__init__

    def patched(self, *a, **kw):
        init(self, *a, **kw)
        self.s.mount("http://", HTTPAdapter(max_retries=Retry(total=10, connect=10, read=10, status=0, other=10,
                                                              allowed_methods=None, backoff_factor=1)))
    conn.ES.__init__ = patched


def main() -> int:
    _retry()
    es = ES()
    v = es.s.get(es.url, timeout=120).json()
    print(f"ES {v['version']['number']}（{v['cluster_name']}）")
    cleanup(es)
    now0 = pd.Timestamp.now(tz="UTC").floor("min")
    only = sys.argv[1] if len(sys.argv) > 1 else ""   # a 或 b：只跑一段
    if only != "a":
        part_b(es, now0)   # 先跑：metrics 是 TSDS，資料流建立後只收建立前 2 小時內的時間
    if only != "b":
        part_a(es, now0)
    fails = [r for r in results if not r[2]]
    print(f"\n{len(results) - len(fails)} PASS、{len(fails)} FAIL")
    for g, n, _, d in fails:
        print(f"  FAIL [{g}] {n}：{d}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

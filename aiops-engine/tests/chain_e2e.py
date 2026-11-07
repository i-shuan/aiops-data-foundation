"""跨服務事件聚合（一層父子關係）的端到端測試（docs/pdca/phase3-locate/2026-10-cross-service-aggregation.md）。

需要自己的 ES（跑過 telemetry/scripts/elasticsearch-setup.sh）。不要對共用 cluster 跑：會刪 cs- 開頭服務的 trace、異常、告警、事故。
  ES_URL=http://127.0.0.1:31511 ES_AUTH=x:x python -X utf8 -m tests.chain_e2e      （在 aiops-engine/ 底下；LLM 固定 template）
可重跑：每次先刪 cs- 開頭的資料，時間以「現在」為準重新產生。約 3 分鐘，每項印 PASS／FAIL。

合成 trace 直接寫進 traces-generic.otel-it（跟 Collector 寫出來的形狀一樣，不經 Collector），再跑一次 detector trace-merge 合成
traces-merged-*（排程器平常每分鐘跑；根因分析、「誰呼叫誰」都讀 merged）；異常直接寫進 aiops-anomalies
（不跑 detect：測的是併件，不是判斷），再跑 `investigator alert --config tests/rca_chain.yaml --at …`，看 aiops-alerts 開了幾件。

服務：cs-portal → cs-mes-api → cs-wip-worker → oracle；cs-report → oracle（跟誰都沒有呼叫關係）。
情境（每個相隔 1 小時；close_after 15m，上一個情境的件在下一個情境前已結束）：
  連鎖 ×5（KPI 聚合率）：cs-wip-worker（下游）連線池逾時、cs-mes-api（上游）同時段變慢。5 次分別是：同一個 5 分鐘、上游先寫入、
    上游晚一個 5 分鐘、分兩輪跑（下游先進 ES 再來上游）、分兩輪跑（上游先進 ES 再來下游：上游那件要標 merged_into）。
  無關 ×5（誤併率）：cs-wip-worker 與 cs-report 同時出事（trace 裡沒有邊）。
  多層 ×1：cs-portal、cs-mes-api、cs-wip-worker 同時出事。
  時間不重疊 ×1：cs-wip-worker 0 分、cs-mes-api 35 分。

預期（先寫、看到結果不放寬）：
  連鎖：每次只有 1 件開著、是 cs-wip-worker、upstream_services == [cs-mes-api]、根因分析以 cs-wip-worker 跑。聚合率 ≥ 80%（≥ 4/5）。
  無關：2 件、都沒有 upstream_services、沒有 merged_into。誤併率 0（0/5）。
  多層：2 件：cs-wip-worker（連帶 cs-mes-api）、cs-portal 自己一件（只併一層）。
  不重疊：cs-wip-worker 那件結束、cs-mes-api 另開一件、沒有連帶。
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from common.infra.es import ES
from tests.aiops_es import AiopsES     # engine 自己的資料在 PostgreSQL：aiops-* 的請求轉過去
from tests.aiops_es import ANOMALY_MAPPING

CFG = "tests/rca_chain.yaml"
TRACES = "traces-generic.otel-it"
PORTAL, MES, WIP, REPORT = "cs-portal", "cs-mes-api", "cs-wip-worker", "cs-report"
DEPT = {PORTAL: "D-MES", MES: "D-MES", WIP: "D-WIP", REPORT: "D-RPT"}
ROUTE = {PORTAL: "/home", MES: "/lots/{id}", REPORT: "/report"}
PER_MIN = 12
PY = sys.executable
results: list[tuple[str, str, bool, str]] = []


def check(group: str, name: str, ok: bool, detail: str = ""):
    results.append((group, name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"：{detail}" if detail else ""), flush=True)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 合成資料 ──

def hexid(rng: random.Random, n: int) -> str:
    return f"{rng.getrandbits(n * 4):0{n}x}"


def span_doc(tid, sid, parent, svc, name, kind, start_ms, dur_ms, *, error=False, attrs=None) -> dict:
    d = {"@timestamp": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds"),
         "data_stream": {"type": "traces", "dataset": "generic.otel", "namespace": "it"},
         "trace_id": tid, "span_id": sid, "name": name, "kind": kind, "duration": int(dur_ms * 1e6),
         "resource": {"attributes": {"service.name": svc, "k8s.namespace.name": "cs"}},
         "scope": {"name": "cs", "attributes": {"tailsampling.policy": "random"}}, "attributes": dict(attrs or {})}
    if parent:
        d["parent_span_id"] = parent
    if error:
        d["status"] = {"code": "Error"}
    return d


def chain_request(rng: random.Random, t0: float, *, slow: bool, with_portal: bool) -> list[dict]:
    """[cs-portal GET /home →] cs-mes-api GET /lots/{id} → cs-wip-worker GET /wip → oracle。
    slow：wip 查 DB 1.3～1.7 秒、1/4 出錯（連線池逾時），一路傳回入口。"""
    tid = hexid(rng, 32)
    ids = iter(hexid(rng, 16) for _ in range(8))
    db = rng.uniform(1300, 1700) if slow else rng.uniform(20, 40)
    err = slow and rng.random() < 0.25
    docs, t, parent = [], t0, None
    if with_portal:
        p_s, p_c = next(ids), next(ids)
        docs.append(span_doc(tid, p_s, None, PORTAL, "GET /home", "Server", t0, db + 12, error=err,
                             attrs={"http.request.method": "GET", "http.route": "/home"}))
        docs.append(span_doc(tid, p_c, p_s, PORTAL, "GET", "Client", t0 + 1, db + 10, error=err, attrs={"peer.service": MES}))
        parent, t = p_c, t0 + 2
    mes_s, mes_c, wip_s = next(ids), next(ids), next(ids)
    docs.append(span_doc(tid, mes_s, parent, MES, "GET /lots/{id}", "Server", t, db + 8, error=err,
                         attrs={"http.request.method": "GET", "http.route": "/lots/{id}"}))
    docs.append(span_doc(tid, mes_c, mes_s, MES, "GET", "Client", t + 1, db + 6, error=err, attrs={"peer.service": WIP}))
    docs.append(span_doc(tid, wip_s, mes_c, WIP, "GET /wip", "Server", t + 2, db + 4, error=err,
                         attrs={"http.request.method": "GET", "http.route": "/wip"}))
    docs.append(span_doc(tid, next(ids), wip_s, WIP, "SELECT LOT_HISTORY", "Client", t + 3, db, error=err, attrs={"db.system": "oracle"}))
    return docs


def report_request(rng: random.Random, t0: float) -> list[dict]:
    tid, s, c = hexid(rng, 32), hexid(rng, 16), hexid(rng, 16)
    db = rng.uniform(50, 90)
    return [span_doc(tid, s, None, REPORT, "GET /report", "Server", t0, db + 3, attrs={"http.request.method": "GET", "http.route": "/report"}),
            span_doc(tid, c, s, REPORT, "SELECT", "Client", t0 + 1, db, attrs={"db.system": "oracle"})]


def traffic(rng: random.Random, t: datetime, *, with_portal: bool) -> list[dict]:
    """t−25 分～t+15 分：每分鐘 PER_MIN 個連鎖請求（t～t+10 分慢）、PER_MIN/2 個 cs-report 請求。
    t−25 分起就有正常的流量：根因分析一週前沒資料時拿「出事前一段」當平常（ref_gap 10m）。"""
    docs = []
    for m in range(-25, 15):
        base = (t + timedelta(minutes=m)).timestamp() * 1000
        for _ in range(PER_MIN):
            docs += chain_request(rng, base + rng.uniform(0, 55_000), slow=0 <= m < 10, with_portal=with_portal)
        for _ in range(PER_MIN // 2):
            docs += report_request(rng, base + rng.uniform(0, 55_000))
    return docs


def anomaly(_id: str, svc: str, at: datetime, kind: str) -> tuple[str, dict]:
    """kind pool：規則 critical（連線池逾時，鑰匙是 service）；lat：baseline 的 anomaly（p95 延遲，鑰匙是 service.name＋API）。"""
    base = {"@timestamp": at.isoformat(), "detected_at": at.isoformat(), "dept": DEPT[svc], "group": "it"}
    if kind == "pool":
        return _id, {**base, "series": "rule.db.pool_timeout", "level": "rule", "severity": "critical", "keys": {"service": svc},
                     "value": 12.0, "message": f"連線池逾時：{svc}（12，門檻 >= 5）"}
    return _id, {**base, "series": "http.latency_p95_s", "level": "dow_hour", "status": "anomaly", "direction": "high", "streak": 2,
                 "keys": {"resource.attributes.service.name": svc, "attributes.http.request.method": "GET", "attributes.http.route": ROUTE[svc]},
                 "value": 1.6, "median": 0.05, "z": 30.0}


# ── ES ──

def bulk_create(es: ES, index: str, docs: list[dict], chunk: int = 4000) -> int:
    n = 0
    for i in range(0, len(docs), chunk):
        lines = []
        for d in docs[i:i + chunk]:
            lines += [json.dumps({"create": {"_index": index}}), json.dumps(d)]
        r = es.s.post(f"{es.url}/_bulk", data="\n".join(lines) + "\n", headers={"Content-Type": "application/x-ndjson"}, timeout=600)
        r.raise_for_status()
        items = r.json()["items"]
        bad = [it["create"] for it in items if it["create"].get("error") and it["create"]["error"].get("type") != "version_conflict_engine_exception"]
        if bad:
            raise RuntimeError(f"bulk 有 {len(bad)} 筆被拒：{bad[:2]}")
        n += len(items)
    return n


def cleanup(es: ES):
    dbq = "_delete_by_query?conflicts=proceed&refresh=true&ignore_unavailable=true&allow_no_indices=true&expand_wildcards=all"
    for index, field in [("traces-*.otel-*", "resource.attributes.service.name"), ("traces-merged-*", "root.service"),
                         ("aiops-anomalies", "keys.service"),
                         ("aiops-anomalies", "keys.resource.attributes.service.name"), ("aiops-alerts", "service"),
                         ("aiops-incidents", "services")]:
        if index.startswith("aiops-"):                  # engine 自己的資料在 PostgreSQL（tests/aiops_es.py 轉過去）
            es.req("POST", f"/{index}/{dbq}", {"query": {"prefix": {field: "cs-"}}})
        else:
            es.s.post(f"{es.url}/{index}/{dbq}", json={"query": {"prefix": {field: "cs-"}}}, timeout=600)


def refresh(es: ES, pattern: str):
    es.s.post(f"{es.url}/{pattern}/_refresh?ignore_unavailable=true&allow_no_indices=true&expand_wildcards=all", timeout=120)


def merge_traces() -> None:
    """寫進 raw 的 span 合成一個請求一筆（traces-merged-*）：trace-merge 依 event.ingested 找新寫進去的 trace，
    合成情境是幾小時前的時間，要從寫入當下往回合。trace-merge 只合到「現在 − settle_seconds（10 秒）」：剛寫完要先等過這段。"""
    import time
    time.sleep(15)
    r = subprocess.run([PY, "-X", "utf8", "-m", "main", "detector", "trace-merge", "--config", "configs/baseline/trace.yaml", "--window", "30min"],
                       capture_output=True, text=True, encoding="utf-8", cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print("  " + (r.stdout.strip().splitlines() or ["trace-merge 沒有輸出"])[-1] + ("" if r.returncode == 0 else f"（rc={r.returncode}：{r.stderr[-300:]}）"))


def run_alert(at: datetime) -> subprocess.CompletedProcess:
    r = subprocess.run([PY, "-X", "utf8", "-m", "main", "investigator", "alert", "--config", CFG, "--at", iso(at)], capture_output=True, text=True,
                       encoding="utf-8", env={**os.environ, "LLM_PROVIDER": "template", "PYTHONUTF8": "1"})
    print(f"    $ investigator alert --at {iso(at)}")
    print("    " + r.stdout.strip().replace("\n", "\n    "))
    if r.returncode:
        print("    stderr:", r.stderr[-1500:])
    return r


def alerts_in(es: ES, t: datetime) -> list[dict]:
    """這個情境時段（t−15 分～t+55 分開始）cs- 服務的件，依開始時間排。"""
    refresh(es, "aiops-alerts")
    body = {"size": 50, "sort": [{"started_at": "asc"}], "query": {"bool": {"filter": [
        {"term": {"category": "service"}}, {"prefix": {"service": "cs-"}},
        {"range": {"started_at": {"gte": (t - timedelta(minutes=15)).isoformat(), "lte": (t + timedelta(minutes=55)).isoformat()}}}]}}}
    return [h["_source"] for h in es.search("aiops-alerts", body)["hits"]["hits"]]


def describe(als: list[dict]) -> str:
    parts = []
    for a in als:
        extra = ""
        if a.get("upstream_services"):
            extra += "，連帶 " + "/".join(a["upstream_services"])
        if a.get("merged_into"):
            extra += f"，併進 {a['merged_into']}"
        parts.append(f"{a['service']}（{a['status']}{extra}）")
    return f"{len(als)} 筆：" + "、".join(parts) if parts else "0 筆"


# ── 情境 ──
# (名稱, 類型, 異常 [(服務, 種類, 第幾分鐘)], 跑 alert 的時間（分鐘）, 流量含 cs-portal)
SCENARIOS = [
    ("連鎖 1：同一個 5 分鐘（下游先）", "chain", [(WIP, "pool", 0), (MES, "lat", 1), (WIP, "pool", 5), (MES, "lat", 6)], [12], False),
    ("連鎖 2：同一個 5 分鐘（上游先）", "chain", [(MES, "lat", 0), (WIP, "pool", 1), (MES, "lat", 5), (WIP, "pool", 6)], [12], False),
    ("連鎖 3：上游晚一個 5 分鐘", "chain", [(WIP, "pool", 0), (WIP, "pool", 5), (MES, "lat", 5), (MES, "lat", 10)], [12], False),
    ("連鎖 4：分兩輪，下游先進 ES", "chain", [(WIP, "pool", 0), (WIP, "pool", 5), (MES, "lat", 5)], [3, 12], False),
    ("連鎖 5：分兩輪，上游先進 ES", "chain", [(MES, "lat", 0), (WIP, "pool", 5), (WIP, "pool", 10)], [3, 12], False),
    ("無關 1：wip-worker 與 report（下游先）", "unrelated", [(WIP, "pool", 0), (REPORT, "lat", 1), (WIP, "pool", 5), (REPORT, "lat", 6)], [12], False),
    ("無關 2：report 先", "unrelated", [(REPORT, "lat", 0), (WIP, "pool", 1), (REPORT, "lat", 5), (WIP, "pool", 6)], [12], False),
    ("無關 3：report 晚一個 5 分鐘", "unrelated", [(WIP, "pool", 0), (WIP, "pool", 5), (REPORT, "lat", 5), (REPORT, "lat", 10)], [12], False),
    ("無關 4：分兩輪", "unrelated", [(WIP, "pool", 0), (WIP, "pool", 5), (REPORT, "lat", 5)], [3, 12], False),
    ("無關 5：mes-api 與 report（都是上游端）", "unrelated", [(MES, "lat", 0), (REPORT, "lat", 1), (MES, "lat", 5), (REPORT, "lat", 6)], [12], False),
    ("多層：portal → mes-api → wip-worker 同時出事", "multi",
     [(WIP, "pool", 0), (MES, "lat", 1), (PORTAL, "lat", 2), (WIP, "pool", 5), (MES, "lat", 6), (PORTAL, "lat", 7)], [12], True),
    ("時間不重疊：上游晚 35 分", "gap", [(WIP, "pool", 0), (WIP, "pool", 5), (MES, "lat", 35), (MES, "lat", 40)], [45], False),
]


def evaluate(kind: str, als: list[dict]) -> bool:
    opened = [a for a in als if a["status"] == "open"]
    by = {a["service"]: a for a in als}
    ups = {a["service"]: a.get("upstream_services") or [] for a in als}
    merged = [a for a in als if a.get("merged_into")]
    if kind == "chain":
        return len(opened) == 1 and opened[0]["service"] == WIP and ups[WIP] == [MES]
    if kind == "unrelated":
        return len(opened) == 2 and len(by) == 2 and not any(ups.values()) and not merged
    if kind == "multi":
        return len(opened) == 2 and {a["service"] for a in opened} == {WIP, PORTAL} and ups[WIP] == [MES] and ups[PORTAL] == []
    if kind == "gap":
        return (set(by) == {WIP, MES} and by[WIP]["status"] == "resolved" and by[MES]["status"] == "open"
                and not any(ups.values()) and not merged)
    raise ValueError(kind)


def main() -> int:
    es = AiopsES(ES())
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    base = now - timedelta(minutes=now.minute % 5) - timedelta(hours=len(SCENARIOS) + 2)
    print(f"== 0. 清掉上次的 cs- 資料；情境從 {iso(base)} 起、每小時一個")
    cleanup(es)
    es.ensure_index("aiops-anomalies", ANOMALY_MAPPING)
    rng = random.Random(29)
    traces, anoms = [], []
    for i, (name, kind, events, _, with_portal) in enumerate(SCENARIOS):
        t = base + timedelta(hours=i)
        traces += traffic(rng, t, with_portal=with_portal)
        anoms += [anomaly(f"cs-{i}-{svc}-{m}", svc, t + timedelta(minutes=m), k) for svc, k, m in events]
    n = bulk_create(es, TRACES, traces)
    es.bulk_index("aiops-anomalies", anoms)
    refresh(es, "traces-*.otel-*")
    refresh(es, "aiops-anomalies")
    print(f"  trace span {n} 筆、異常 {len(anoms)} 筆")
    merge_traces()
    refresh(es, "traces-merged-*")

    per_kind: dict[str, list[bool]] = {}
    chain_first: dict | None = None
    for i, (name, kind, events, runs, _) in enumerate(SCENARIOS):
        t = base + timedelta(hours=i)
        print(f"== {i + 1}. {name}（{iso(t)}）")
        for m in runs:
            run_alert(t + timedelta(minutes=m))
        als = alerts_in(es, t)
        ok = evaluate(kind, als)
        per_kind.setdefault(kind, []).append(ok)
        check(kind, name, ok, describe(als))
        if kind == "chain" and ok and chain_first is None:
            chain_first = next(a for a in als if a["status"] == "open")
            chain_first["_mes_ids"] = [f"cs-{i}-{svc}-{m}" for svc, _, m in events if svc == MES]
        if name.startswith("連鎖 5") and ok:
            mes = next((a for a in als if a["service"] == MES), None)
            wip = next(a for a in als if a["status"] == "open")
            check("chain", "連鎖 5：上游那件標 resolved、merged_into 指到下游那件",
                  bool(mes) and mes["status"] == "resolved" and mes.get("merged_into") == wip["alert_id"], describe(als))
            inc = es.req("GET", f"/aiops-incidents/_doc/{mes['alert_id']}", ok=(200, 404)) if mes else {}
            check("chain", "連鎖 5：上游那件開的事故改成 merged", inc.get("found") and inc["_source"].get("status") == "merged",
                  json.dumps({k: inc.get("_source", {}).get(k) for k in ("status", "merged_into")}, ensure_ascii=False))
            check("chain", "連鎖 5：下游那件的異常 id 含上游那件的", bool(mes) and set(mes["anomaly_ids"]) <= set(wip["anomaly_ids"]))

    print("== 綜合檢查")
    if chain_first:
        al = chain_first
        check("chain", "標題看得出「下游出事，連帶上游」", "；連帶上游 cs-mes-api" in (al.get("title") or ""), al.get("title", ""))
        check("chain", "摘要第一行也有", (al.get("summary") or "").splitlines()[0].endswith("連帶上游 cs-mes-api"), (al.get("summary") or "").splitlines()[0])
        f = al.get("findings") or {}
        check("chain", "根因分析以下游服務（cs-wip-worker）跑", f.get("service") == WIP and "# 根因分析：cs-wip-worker" in (al.get("report") or ""),
              f"findings.service={f.get('service')}")
        slow = [s["segment"] for s in f.get("segments", [])[:1]]
        check("chain", "報告最慢的那段是 wip-worker 查 Oracle", bool(slow) and slow[0].startswith(f"{WIP} › SELECT LOT_HISTORY → oracle"), str(slow))
        check("chain", "件送下游的 dept（D-WIP）", al.get("dept") == "D-WIP", str(al.get("dept")))
        refresh(es, "aiops-anomalies")
        tagged = [es.req("GET", f"/aiops-anomalies/_doc/{i}")["_source"].get("alert_id") for i in al["_mes_ids"]]
        check("chain", "上游的異常寫回了下游那件的 alert_id", all(x == al["alert_id"] for x in tagged), str(tagged))
        inc = es.req("GET", f"/aiops-incidents/_doc/{al['alert_id']}", ok=(200, 404))
        src = inc.get("_source") or {}
        check("chain", "事故的 services 只有下游、upstream_services 另記",
              src.get("services") == [WIP] and src.get("upstream_services") == [MES], json.dumps({k: src.get(k) for k in ("services", "upstream_services")}))
    else:
        check("chain", "沒有任何一次連鎖情境併成一件，綜合檢查跳過", False)
    ids = [h["_source"].get("alert_id") for h in es.search("aiops-alerts", {"size": 10000, "query": {"prefix": {"service": "cs-"}}})["hits"]["hits"]]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    check("grafana", "一件一筆（Grafana 一件一則）：沒有重複的 alert_id", not dup, str(dup))
    hb = es.req("GET", "/aiops-alerts/_doc/heartbeat", ok=(200, 404))
    check("grafana", "心跳照寫", hb.get("found") and hb["_source"]["status"] == "ok")

    print("\n== KPI")
    agg, n_chain = sum(per_kind.get("chain", [])), len(per_kind.get("chain", []))
    fm, n_un = sum(not x for x in per_kind.get("unrelated", [])), len(per_kind.get("unrelated", []))
    check("KPI", f"聚合率 ≥ 80%（連鎖 {n_chain} 次，併成 1 件 {agg} 次 = {agg / max(n_chain, 1):.0%}）", n_chain >= 5 and agg / n_chain >= 0.8)
    check("KPI", f"誤併率 0（無關 {n_un} 次，被併 {fm} 次）", n_un >= 5 and fm == 0)
    print(f"\n== 結果")
    for g, name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  [{g}] {name}" + (f"｜{detail}" if detail and not ok else ""))
    fail = sum(not r[2] for r in results)
    print(f"\n{len(results) - fail} PASS、{fail} FAIL")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

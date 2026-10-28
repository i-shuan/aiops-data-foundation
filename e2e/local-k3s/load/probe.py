# 壓測的量測端，跑在 load-test 的 probe Pod 裡（run-stress.sh 用 kubectl exec 叫）。在 cluster 裡直接連 ES 和
# Collector 的 :8888，不用 port-forward。
#   start  <情境>  ：背景每 2 秒記一次：各 Collector 的 CPU、記憶體、佇列、失敗／拒收計數，ES 的 CPU 與寫入拒絕，
#                    以及各 run 在 ES 裡每一秒（依資料時間）有幾筆（算 metrics、trace 的延遲用）
#   wait   <情境> <最多幾秒>：等 ES 收齊（收到 ≥ Collector 收下的筆數），或 60 秒都沒有增加
#   report <情境>  ：停止記錄，印結果表
# start 的設定從 stdin 讀 JSON：{"runs": [{"run":..,"kind":"log|metric|trace|prom"}], "collectors": [{"name":..,"ip":..}]}
# wait、report 從 stdin 讀產生器的結果（每行一個 {"result": {...}}）。
import base64, json, math, os, signal, subprocess, sys, time, urllib.request

ES = os.environ.get("ES_URL", "http://elasticsearch.es-test.svc:9200")
AUTH = "Basic " + base64.b64encode(os.environ.get("ES_AUTH", "elastic:changeme-test-only").encode()).decode()
DIR = "/tmp/probe"

# 每種資料在 ES 的位置、怎麼認出是哪個 run、用哪些欄位判斷「同一筆」
KINDS = {
    "log": {"index": "k8s-*", "run": "app.run", "exists": "app.seq", "keys": ["app.gen", "app.seq"]},
    "metric": {"index": "metrics-*.otel-*", "run": "attributes.run", "exists": "metrics.loadtest.gauge",
               "keys": ["attributes.g", "attributes.s", "@timestamp"]},
    "trace": {"index": "traces-*.otel-*", "run": "attributes.run", "exists": "span_id", "keys": ["trace_id", "span_id"]},
    "prom": {"index": "metrics-prometheusreceiver.otel-*", "run": "attributes.run", "exists": "metrics.loadtest_card",
             "keys": ["attributes.id", "@timestamp"]},
}
BAD = ("failed", "refused", "dropped", "too_early", "enqueue_failed")


def es(path, body=None, method=None, timeout=120):
    req = urllib.request.Request(ES + path, data=None if body is None else json.dumps(body).encode(),
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers={"Authorization": AUTH, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def query(k, run):
    return {"bool": {"filter": [{"term": {k["run"]: run}}, {"exists": {"field": k["exists"]}}]}}


def scrape(ip):
    out = {}
    with urllib.request.urlopen("http://%s:8888/metrics" % ip, timeout=5) as r:
        for line in r.read().decode().splitlines():
            if not line or line[0] == "#":
                continue
            name, _, val = line.rpartition(" ")
            try:
                out[name] = float(val)
            except ValueError:
                pass
    return out


def refresh(kinds):
    for idx in sorted({KINDS[k]["index"] for k in kinds}):
        try:
            es("/%s/_refresh?ignore_unavailable=true&allow_no_indices=true" % idx, method="POST", timeout=30)
        except Exception:  # noqa: BLE001 ES 停機中
            pass


# ---------------------------------------------------------------- 背景記錄
def sampler(name, cfg):
    f = open("%s/%s.jsonl" % (DIR, name), "a")
    while True:
        t = time.time()
        rec = {"t": t, "col": {}, "es": None, "hist": {}}
        for c in cfg["collectors"]:
            try:
                m = scrape(c["ip"])
                rec["col"][c["name"]] = {k: v for k, v in m.items() if k.startswith(
                    ("otelcol_process_cpu_seconds", "otelcol_process_memory_rss", "otelcol_exporter_queue_size")) or
                    any(b in k for b in BAD) or "_sent_" in k or "_accepted_" in k}
            except Exception as e:  # noqa: BLE001 Collector 重啟中
                rec["col"][c["name"]] = {"error": str(e)[:80]}
        try:
            n = es("/_nodes/stats/process,thread_pool", timeout=10)["nodes"]
            nd = next(iter(n.values()))
            rec["es"] = {"cpu": nd["process"]["cpu"]["percent"], "rejected": nd["thread_pool"]["write"]["rejected"]}
        except Exception:  # noqa: BLE001
            pass
        refresh([r["kind"] for r in cfg["runs"]])
        for r in cfg["runs"]:
            if r["kind"] == "log":
                continue  # log 用 event.ingested 算，準到毫秒
            k = KINDS[r["kind"]]
            try:
                res = es("/%s/_search?ignore_unavailable=true&allow_no_indices=true" % k["index"], {
                    "size": 0, "track_total_hits": True, "query": query(k, r["run"]),
                    "aggs": {"h": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1s", "min_doc_count": 1}}}},
                    timeout=30)
                rec["hist"][r["run"]] = {str(b["key"] // 1000): b["doc_count"] for b in res["aggregations"]["h"]["buckets"]}
            except Exception:  # noqa: BLE001
                pass
        f.write(json.dumps(rec) + "\n")
        f.flush()
        time.sleep(max(0.0, 2 - (time.time() - t)))


def cmd_start(name):
    os.makedirs(DIR, exist_ok=True)
    cfg = json.load(sys.stdin)
    json.dump(cfg, open("%s/%s.cfg" % (DIR, name), "w"))
    for ext in ("jsonl",):
        p = "%s/%s.%s" % (DIR, name, ext)
        if os.path.exists(p):
            os.remove(p)
    p = subprocess.Popen([sys.executable, __file__, "_sample", name], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=open("%s/%s.err" % (DIR, name), "w"))
    open("%s/%s.pid" % (DIR, name), "w").write(str(p.pid))
    print("probe %s：開始記錄（pid %d）" % (name, p.pid))


# ---------------------------------------------------------------- 結果
def results():
    out = {}
    for line in sys.stdin:
        line = line.strip()
        if '"result"' not in line:
            continue
        try:
            r = json.loads(line[line.index("{"):])["result"]
        except (ValueError, KeyError):
            continue
        a = out.setdefault(r["run"], {"kind": r["mode"], "sent": 0, "refused": 0, "pods": 0, "errors": {}, "extra": {}})
        a["sent"] += r.get("sent", 0)
        a["refused"] += r.get("refused", 0)
        a["pods"] += 1
        for e, n in (r.get("errors") or {}).items():
            a["errors"][e] = a["errors"].get(e, 0) + n
        for e in ("scrapes", "distinct_series"):
            if e in r:
                a["extra"][e] = a["extra"].get(e, 0) + r[e]
    return out


def count(kind, run):
    k = KINDS[kind]
    return es("/%s/_count?ignore_unavailable=true&allow_no_indices=true" % k["index"], {"query": query(k, run)})["count"]


def cmd_wait(name, limit):
    sent = results()
    kinds = [v["kind"] for v in sent.values()]
    t0 = time.time()
    last, last_change = None, time.time()
    while time.time() - t0 < limit:
        refresh(kinds)
        got = {}
        try:
            got = {run: count(v["kind"], run) for run, v in sent.items()}
        except Exception:  # noqa: BLE001 ES 還沒好
            pass
        if got and all(got[r] >= v["sent"] - v["refused"] for r, v in sent.items()):
            print("收齊（%d 秒）" % (time.time() - t0))
            return
        if got != last:
            last, last_change = got, time.time()
        elif time.time() - last_change > 60:
            print("60 秒沒有增加，停止等待（%d 秒）：%s" % (time.time() - t0, got))
            return
        time.sleep(5)
    print("等到上限 %d 秒" % limit)


def unique(kind, run):
    k = KINDS[kind]
    after, n = None, 0
    while True:
        comp = {"size": 50000, "sources": [{f.replace(".", "_"): {"terms": {"field": f}}} for f in k["keys"]]}
        if after:
            comp["after"] = after
        res = es("/%s/_search?ignore_unavailable=true&allow_no_indices=true" % k["index"],
                 {"size": 0, "query": query(k, run), "aggs": {"u": {"composite": comp}}}, timeout=300)
        b = res["aggregations"]["u"]
        n += len(b["buckets"])
        after = b.get("after_key")
        if not after or not b["buckets"]:
            return n


def log_lag(run):
    k = KINDS["log"]
    res = es("/%s/_search" % k["index"], {
        "size": 0, "query": query(k, run),
        "runtime_mappings": {"lag": {"type": "long", "script": "emit(doc['event.ingested'].value.toInstant().toEpochMilli()"
                                     " - doc['@timestamp'].value.toInstant().toEpochMilli())"}},
        "aggs": {"p": {"percentiles": {"field": "lag", "percents": [50, 95]}}, "mx": {"max": {"field": "lag"}}}})
    p = res["aggregations"]["p"]["values"]
    return p["50.0"] and p["50.0"] / 1000, p["95.0"] and p["95.0"] / 1000, (res["aggregations"]["mx"]["value"] or 0) / 1000


def poll_lag(run, samples):
    """每次記錄時各秒的筆數，新增的那些筆的延遲 ≈ 這次與上次記錄的中間點 − 資料那一秒的中間點（誤差約 ±1 秒）"""
    prev, prev_t, lags = {}, None, []
    for s in samples:
        h = s["hist"].get(run)
        if h is None:
            continue
        mid = s["t"] if prev_t is None else (s["t"] + prev_t) / 2
        for sec, c in h.items():
            d = c - prev.get(sec, 0)
            if d > 0:
                lags.append((max(0.0, mid - (int(sec) + 0.5)), d))
        prev, prev_t = h, s["t"]
    if not lags:
        return None, None, None
    lags.sort()
    tot = sum(d for _, d in lags)

    def pct(p):
        acc = 0
        for v, d in lags:
            acc += d
            if acc >= tot * p:
                return v
        return lags[-1][0]
    return pct(0.5), pct(0.95), lags[-1][0]


def fmt_s(v):
    return "-" if v is None else "%.1fs" % v


def cmd_report(name):
    sent = results()
    pid = int(open("%s/%s.pid" % (DIR, name)).read())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    samples = [json.loads(l) for l in open("%s/%s.jsonl" % (DIR, name)) if l.strip()]
    refresh([v["kind"] for v in sent.values()])
    rows = []
    print("\n== %s：每一條資料" % name)
    print("| run | 種類 | Pod | 送出 | Collector 拒收 | ES 收到 | 遺失 | 重複 | 延遲 p50／p95／最大 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for run, v in sent.items():
        try:
            got = count(v["kind"], run)
            u = unique(v["kind"], run)
            lag = log_lag(run) if v["kind"] == "log" else poll_lag(run, samples)
        except OSError as e:  # ES 忙到查不動（逾時）：這一列照印，標出來
            print("| %s | %s | %d | %d | %d | ES 查詢失敗：%s |  |  |  |" % (run, v["kind"], v["pods"], v["sent"], v["refused"], e))
            continue
        lost = v["sent"] - v["refused"] - u
        row = dict(run=run, kind=v["kind"], pods=v["pods"], sent=v["sent"], refused=v["refused"], received=got,
                   lost=lost, dup=got - u, p50=lag[0], p95=lag[1], max=lag[2], errors=v["errors"], extra=v["extra"])
        rows.append(row)
        print("| %s | %s | %d | %d | %d%s | %d | %d | %d | %s／%s／%s |" % (
            run, v["kind"], v["pods"], v["sent"], v["refused"], (" " + json.dumps(v["errors"])) if v["errors"] else "",
            got, lost, got - u, fmt_s(lag[0]), fmt_s(lag[1]), fmt_s(lag[2])))
        if v["extra"]:
            print("|  | %s |  |  |  |  |  |  |  |" % json.dumps(v["extra"]))

    print("\n== %s：Collector 與 ES（每 2 秒記一次，%d 筆）" % (name, len(samples)))
    print("| 誰 | CPU 平均／最高（1 = 1 核） | 記憶體 開始／最高 | 佇列最高 | 失敗／拒收／丟掉的計數（增加量） |")
    print("|---|---|---|---|---|")
    names = sorted({c for s in samples for c in s["col"]})
    res = {}
    for c in names:
        pts = [(s["t"], s["col"][c]) for s in samples if c in s["col"] and "error" not in s["col"][c]]
        if len(pts) < 2:
            print("| %s | 沒有資料 |  |  |  |" % c)
            continue
        cpu = [(t, m.get("otelcol_process_cpu_seconds", 0)) for t, m in pts]
        avg = (cpu[-1][1] - cpu[0][1]) / (cpu[-1][0] - cpu[0][0]) if cpu[-1][1] >= cpu[0][1] else None
        peak = 0.0
        for i in range(len(cpu)):  # 10 秒窗的最高
            j = next((j for j in range(i, len(cpu)) if cpu[j][0] - cpu[i][0] >= 10), None)
            if j is not None and cpu[j][1] >= cpu[i][1]:
                peak = max(peak, (cpu[j][1] - cpu[i][1]) / (cpu[j][0] - cpu[i][0]))
        rss = [m.get("otelcol_process_memory_rss", 0) / 2**20 for _, m in pts]
        qmax = {}
        for _, m in pts:
            for k, val in m.items():
                if k.startswith("otelcol_exporter_queue_size"):
                    qmax[k] = max(qmax.get(k, 0), val)
        q = ", ".join("%s=%d" % (k.split("{")[1].rstrip("}").replace('"', ""), val) for k, val in qmax.items() if val > 0) or "0"
        bad = {}
        first, last = pts[0][1], pts[-1][1]
        for k, val in last.items():
            if any(b in k for b in BAD):
                d = val - first.get(k, 0)
                if d > 0:
                    bad[k.replace("otelcol_", "")] = int(d)
        restarts = sum(1 for s in samples if c in s["col"] and "error" in s["col"][c])
        print("| %s | %s／%.2f | %dMB／%dMB | %s | %s%s |" % (
            c, "-" if avg is None else "%.2f" % avg, peak, rss[0], max(rss), q,
            json.dumps(bad, ensure_ascii=False) if bad else "0", "（%d 次抓不到，重啟中？）" % restarts if restarts else ""))
        res[c] = dict(cpu_avg=avg, cpu_peak=peak, rss_start=rss[0], rss_max=max(rss), queue_max=qmax, bad=bad)
    esd = [s["es"] for s in samples if s.get("es")]
    if esd:
        print("| ES | CPU 平均 %d%%、最高 %d%%（佔整台機器） |  |  | 寫入被拒（thread pool）+%d |" % (
            sum(e["cpu"] for e in esd) / len(esd), max(e["cpu"] for e in esd), esd[-1]["rejected"] - esd[0]["rejected"]))
    json.dump({"rows": rows, "collectors": res}, open("%s/%s.result.json" % (DIR, name), "w"), default=str)


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "_sample":
        sampler(sys.argv[2], json.load(open("%s/%s.cfg" % (DIR, sys.argv[2]))))
    elif c == "start":
        cmd_start(sys.argv[2])
    elif c == "wait":
        cmd_wait(sys.argv[2], int(sys.argv[3]))
    elif c == "report":
        cmd_report(sys.argv[2])

#!/usr/bin/env python
"""查 Elasticsearch，確認資料有進來、進對部門。由 run.sh 呼叫，也可以單獨跑。

  python tests/obs/check.py routing   數值與 log 每個情境進了哪一份、標籤對不對
  python tests/obs/check.py fault     把 demo-api 弄慢、弄成回錯誤，Elasticsearch 裡看不看得到
  python tests/obs/check.py outage    停掉 Elasticsearch、中間重啟收集程式、再恢復，比筆數與 log 的序號
  python tests/obs/check.py restart   讓一個容器重啟，新舊兩個容器的 log 都要收得到、不漏不重複

只對本機的 k3d cluster obs-test 下指令。查詢是進 Elasticsearch 的 pod 裡用 curl 打的，
密碼留在 pod 的環境變數裡，不會經過這支程式。
全部通過 exit 0，有任何一項不過 exit 1。
"""
import json
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

CTX = "k3d-obs-test"
SITE, CLUSTER = "ctsp", "obs-test"
NS = "resource.attributes.k8s.namespace.name"
LOGS = "logs-*"
# namespace → 應該進哪個部門。(none) 是沒有 namespace 的資料，也就是 server 本身
EXPECT = {
    "demo-tsid": "tsid",
    "demo-bsid": "bsid",
    "fake-none": "platform",
    "fake-typo": "platform",
    "fake-spoof": "bsid",
    "obs": "platform",
    "kube-system": "platform",
    "(none)": "icsd",
}
WINDOW = 120          # 停機測試每一段比幾秒
NEED = 0.95           # 補回來的筆數至少要是平常的幾成
clock_offset = 0.0    # cluster 的時鐘減掉這台機器的時鐘


def kubectl(*args, stdin=None, check=True):
    r = subprocess.run(["kubectl", "--context", CTX] + list(args), input=stdin,
                       capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode:
        raise RuntimeError("kubectl %s：%s" % (" ".join(args[:6]), r.stderr.strip()[-300:]))
    return r.stdout


def es(path, body):
    cmd = ('curl -s -m 20 -u "elastic:$ELASTIC_PASSWORD" -H "Content-Type: application/json" '
           '"localhost:9200/%s" -d @-' % path)
    out = kubectl("-n", "obs", "exec", "-i", "elasticsearch-0", "--", "sh", "-c", cmd, stdin=json.dumps(body))
    doc = json.loads(out)
    if "error" in doc:
        raise RuntimeError("Elasticsearch：%s" % json.dumps(doc["error"], ensure_ascii=False)[:300])
    return doc


def sync_clock():
    global clock_offset
    clock_offset = float(kubectl("-n", "obs", "exec", "elasticsearch-0", "--", "date", "+%s").strip()) - time.time()


def now():
    return time.time() + clock_offset


def between(start, end):
    return {"range": {"@timestamp": {"gte": int(start * 1000), "lt": int(end * 1000), "format": "epoch_millis"}}}


def count(filters, index="metrics-*"):
    return es(index + "/_count", {"query": {"bool": {"filter": filters}}})["count"]


def phrase(text):
    return {"match_phrase": {"body.text": text}}


def has(field):
    return {"exists": {"field": field}}


def term(field, value):
    return {"term": {field: value}}


def show(results):
    for ok, title, detail in results:
        print("  %s  %s" % ("OK  " if ok else "FAIL", title))
        if detail and not ok:
            print("        %s" % detail)
    bad = sum(1 for ok, _, _ in results if not ok)
    print("\n" + ("PASS" if not bad else "FAIL（%d 項）" % bad))
    return 0 if not bad else 1


# ---------------------------------------------------------------- routing

def routing_once():
    r = []
    recent = between(now() - 90, now() + 5)

    def keys(bucket, name):
        return sorted(str(x["key"]) for x in bucket[name]["buckets"])

    sub = {"stream": {"terms": {"field": "data_stream.namespace"}},
           "tenant": {"terms": {"field": "resource.attributes.tenant", "missing": "(沒有)"}},
           "site": {"terms": {"field": "resource.attributes.site", "missing": "(沒有)"}},
           "cluster": {"terms": {"field": "resource.attributes.cluster", "missing": "(沒有)"}},
           "dept": {"terms": {"field": "resource.attributes.dept", "missing": "(沒有)"}},
           "node": {"missing": {"field": "resource.attributes.k8s.node.name"}}}
    doc = es("metrics-*/_search", {"size": 0, "query": {"bool": {"filter": [recent]}},
                                   "aggs": {"ns": {"terms": {"field": NS, "size": 100, "missing": "(none)"},
                                                   "aggs": sub}}})
    seen = {b["key"]: b for b in doc["aggregations"]["ns"]["buckets"]}

    # 每個 namespace 的資料只能在一份資料流裡，而且就是該去的那一份
    for ns, tenant in EXPECT.items():
        b = seen.get(ns)
        name = "server 本身（沒有 namespace）" if ns == "(none)" else ns
        if not b:
            r.append((False, "%-28s → %s" % (name, tenant), "最近 90 秒沒有資料"))
            continue
        got = (keys(b, "stream"), keys(b, "tenant"), keys(b, "site"), keys(b, "cluster"))
        want = ([tenant], [tenant], [SITE], [CLUSTER])
        r.append((got == want, "%-28s → %s（%d 筆）" % (name, tenant, b["doc_count"]),
                  "資料流 %s、tenant %s、site %s、cluster %s" % got))
    extra = sorted(ns for ns, b in seen.items() if ns not in EXPECT and keys(b, "stream") != ["platform"])
    r.append((not extra, "沒列在上面的 namespace 都在 platform", "不在 platform 的：%s" % ", ".join(extra)))

    # 門牌上的其他欄位照原值
    for ns, dept in (("demo-tsid", "D3120"), ("fake-spoof", "D9999"), ("fake-none", "(沒有)")):
        got = keys(seen[ns], "dept") if ns in seen else []
        r.append((got == [dept], "%s 的 dept 是 %s" % (ns, dept), "實際 %s" % got))

    # server 的資料：都帶節點名稱，四大類都有
    if "(none)" in seen:
        r.append((seen["(none)"]["node"]["doc_count"] == 0, "server 的資料都帶節點名稱",
                  "%d 筆沒有 k8s.node.name" % seen["(none)"]["node"]["doc_count"]))
    icsd = [recent, term("data_stream.namespace", "icsd")]
    for label, field in (("CPU", "metrics.system.cpu.time"), ("負載", "metrics.system.cpu.load_average.1m"),
                         ("記憶體", "metrics.system.memory.usage"), ("磁碟", "metrics.system.filesystem.usage"),
                         ("網路", "metrics.system.network.io"), ("節點是不是 Ready", "metrics.k8s.node.condition_ready")):
        n = count(icsd + [has(field)])
        r.append((n > 0, "icsd 有 server 的%s（%d 筆）" % (label, n), "沒有 %s" % field))

    # 每個容器用了多少資源：照 pod 所在 namespace 的門牌，不是照節點
    for ns, tenant in (("demo-tsid", "tsid"), ("demo-bsid", "bsid")):
        for label, field in (("記憶體", "metrics.container.memory.working_set"), ("CPU", "metrics.container.cpu.time"),
                             ("重啟次數", "metrics.k8s.container.restarts")):
            f = [recent, term(NS, ns), has(field)]
            # 直接數「不在這個部門的」。分兩次數全部與對的再相減的話，兩次查詢之間進來的資料會造成誤判
            good = count(f + [term("data_stream.namespace", tenant)])
            wrong = count(f + [{"bool": {"must_not": [term("data_stream.namespace", tenant)]}}])
            r.append((good > 0 and wrong == 0, "%s 容器的%s在 %s（%d 筆）" % (ns, label, tenant, good),
                      "在 %s 的 %d 筆，在別的部門的 %d 筆" % (tenant, good, wrong)))

    # 程式吐的數值，包含分布圖（處理時間）
    for ns, tenant in (("demo-tsid", "tsid"), ("demo-bsid", "bsid")):
        for label, field in (("請求數", "metrics.demo_requests_total"),
                             ("處理時間", "metrics.demo_request_duration_seconds")):
            n = count([recent, term(NS, ns), term("data_stream.namespace", tenant), has(field)])
            r.append((n > 0, "%s 程式的%s在 %s（%d 筆）" % (ns, label, tenant, n), "沒有 %s" % field))

    # 冒充：程式自稱的 tenant / site / cluster 不能留在樣本上
    # fake-spoof 的門牌是 bsid、自稱 tsid；fake-none 沒掛門牌、自稱 tsid
    for ns in ("fake-spoof", "fake-none"):
        spoof = [recent, term(NS, ns)]
        n = count(spoof + [has("metrics.fake_value")])
        r.append((n > 0, "%s 冒充的數值有被抓到（%d 筆）" % (ns, n), "沒有 fake_value，這一項等於沒測"))
        for field in ("attributes.tenant", "attributes.site", "attributes.cluster"):
            n = count(spoof + [has(field)])
            r.append((n == 0, "%s 自稱的 %s 沒有留在樣本上" % (ns, field.split(".")[1]),
                      "%d 筆還帶著 %s" % (n, field)))
    return r


# namespace → log 應該進哪個部門。只列一定有在印 log 的
LOG_EXPECT = {"demo-tsid": "tsid", "demo-bsid": "bsid", "fake-none": "platform", "fake-typo": "platform",
              "fake-spoof": "bsid"}


def logs_once():
    r = []
    recent = between(now() - 90, now() + 5)

    def keys(bucket, name):
        return sorted(str(x["key"]) for x in bucket[name]["buckets"])

    def n(*filters):
        return count([recent] + list(filters), LOGS)

    sub = {name: {"terms": {"field": field, "missing": "(沒有)"}} for name, field in (
        ("stream", "data_stream.namespace"), ("tenant", "resource.attributes.tenant"),
        ("site", "resource.attributes.site"), ("cluster", "resource.attributes.cluster"),
        ("dept", "resource.attributes.dept"))}
    doc = es(LOGS + "/_search", {"size": 0, "query": {"bool": {"filter": [recent]}},
                                 "aggs": {"ns": {"terms": {"field": NS, "size": 100, "missing": "(none)"}, "aggs": sub}}})
    seen = {b["key"]: b for b in doc["aggregations"]["ns"]["buckets"]}
    for ns, tenant in LOG_EXPECT.items():
        b = seen.get(ns)
        if not b:
            r.append((False, "log %-24s → %s" % (ns, tenant), "最近 90 秒沒有 log"))
            continue
        got = (keys(b, "stream"), keys(b, "tenant"), keys(b, "site"), keys(b, "cluster"))
        r.append((got == ([tenant], [tenant], [SITE], [CLUSTER]), "log %-24s → %s（%d 筆）" % (ns, tenant, b["doc_count"]),
                  "資料流 %s、tenant %s、site %s、cluster %s" % got))
    extra = sorted(ns for ns, b in seen.items() if ns not in LOG_EXPECT and keys(b, "stream") != ["platform"])
    r.append((not extra, "log：沒列在上面的 namespace 都在 platform", "不在 platform 的：%s" % ", ".join(extra)))
    for ns, dept in (("demo-tsid", "D3120"), ("fake-spoof", "D9999")):
        got = keys(seen[ns], "dept") if ns in seen else []
        r.append((got == [dept], "log：%s 的 dept 是 %s，跟數值一樣" % (ns, dept), "實際 %s" % got))

    # 每筆都知道是哪個 pod、哪個容器
    for field in ("k8s.pod.name", "k8s.container.name"):
        miss = count([recent, {"bool": {"must_not": [has("resource.attributes." + field)]}}], LOGS)
        r.append((miss == 0, "log：每筆都帶 %s" % field, "%d 筆沒有" % miss))

    # JSON 的拆成欄位，等級與訊息統一
    demo = term(NS, "demo-tsid")
    got = n(demo, term("severity_text", "INFO"), has("attributes.trace_id"), has("attributes.message"))
    r.append((got > 0, "log：JSON 拆成欄位，等級、訊息、trace_id 都查得到（%d 筆）" % got, "demo-tsid 查不到"))
    got = n(term(NS, "fake-typo"), term("severity_text", "WARN"), has("attributes.message"), has("attributes.seq"))
    r.append((got > 0, "log：另一種欄位命名（log.level）也認得等級（%d 筆）" % got, "fake-typo 查不到等級是 WARN 的"))
    got = n(term(NS, "fake-none"), phrase("plain text line"))
    r.append((got > 0, "log：不是 JSON 的照樣收（%d 筆）" % got, "fake-none 的純文字 log 查不到"))

    # 冒充，順便是型別衝突：code 在範例程式是數字，這裡是文字
    spoof = term(NS, "fake-spoof")
    got = n(spoof, term("severity_text", "ERROR"), phrase("E42"))
    r.append((got > 0, "log：欄位型別跟別人不合的照樣進得來（%d 筆）" % got, "fake-spoof 的 log 查不到，可能整筆被拒絕"))
    for field in ("attributes.tenant", "attributes.site", "attributes.cluster"):
        got = n(spoof, has(field))
        r.append((got == 0, "log：fake-spoof 自稱的 %s 沒有留在資料上" % field.split(".")[1], "%d 筆還帶著 %s" % (got, field)))

    # 收集程式自己的不收
    got = n({"prefix": {"resource.attributes.k8s.pod.name": "otel-"}})
    r.append((got == 0, "log：收集程式自己的沒有收", "收到 %d 筆" % got))
    return r


def routing():
    print("=== 分流：每個情境進了哪一份（數值，然後 log）===")
    deadline = time.time() + 240
    while True:
        results = routing_once() + logs_once()
        if all(ok for ok, _, _ in results) or time.time() > deadline:
            return show(results)
        time.sleep(15)


# ---------------------------------------------------------------- fault

def latency(seconds):
    """最近幾秒 demo-tsid 兩支程式的平均處理時間（秒）。從 Elasticsearch 裡的分布圖算。"""
    doc = es("metrics-*/_search", {
        "size": 0,
        "query": {"bool": {"filter": [between(now() - seconds, now() + 5), term(NS, "demo-tsid"),
                                      term("attributes.route", "/work"),
                                      has("metrics.demo_request_duration_seconds")]}},
        "aggs": {"c": {"terms": {"field": "resource.attributes.k8s.container.name"},
                       "aggs": {"avg": {"avg": {"field": "metrics.demo_request_duration_seconds"}}}}}})
    return {b["key"]: b["avg"]["value"] for b in doc["aggregations"]["c"]["buckets"]}


def set_fault(query):
    pod = kubectl("-n", "demo-tsid", "get", "pod", "-l", "app.kubernetes.io/name=demo-api",
                  "-o", "jsonpath={.items[0].metadata.name}").strip()
    kubectl("get", "--raw", "/api/v1/namespaces/demo-tsid/pods/%s:8080/proxy/admin/fault?%s" % (pod, query))


def fault():
    print("=== 弄壞看得到：demo-api 每個請求多睡 400 毫秒 ===")
    r = []
    set_fault("clear=1")
    time.sleep(50)
    before = latency(40)
    try:
        set_fault("latency_ms=400")
        time.sleep(75)
        after = latency(40)
    finally:
        set_fault("clear=1")
    for name in ("demo-api", "demo-web"):
        a, b = before.get(name), after.get(name)
        if a is None or b is None:
            r.append((False, "%s 的處理時間" % name, "Elasticsearch 裡查不到：前 %s、後 %s" % (a, b)))
            continue
        r.append((b - a >= 0.2, "%s 的處理時間 %d ms → %d ms" % (name, a * 1000, b * 1000),
                  "變化不到 200 ms，在 Elasticsearch 裡看不出來"))

    print("=== 弄壞看得到：demo-api 一半的請求回錯誤 ===")
    try:
        set_fault("error_rate=0.5")
        time.sleep(45)
    finally:
        set_fault("clear=1")
    window = [between(now() - 40, now() + 5), term(NS, "demo-tsid")]
    doc = es(LOGS + "/_search", {"size": 5, "query": {"bool": {"filter": window + [
        term("resource.attributes.k8s.container.name", "demo-api"), term("severity_text", "ERROR"),
        has("attributes.trace_id")]}}})
    hits = doc["hits"]["hits"]
    r.append((bool(hits), "demo-api 的錯誤 log 查得到", "最近 40 秒沒有等級是 ERROR 的"))
    if hits:
        tid = hits[0]["_source"]["attributes"]["trace_id"]
        other = count(window + [term("resource.attributes.k8s.container.name", "demo-web"),
                                term("attributes.trace_id", tid)], LOGS)
        r.append((other > 0, "同一個 trace_id（%s…）在 demo-web 的 log 也查得到" % tid[:8], "demo-web 查不到這個 trace_id"))
    return show(r)


# ---------------------------------------------------------------- outage

def samples(start, end):
    """這段時間有幾次抓取進了 Elasticsearch。各挑一個每次抓取剛好一筆的數值來數。"""
    t = between(start, end)
    return {"程式": count([t, term(NS, "demo-tsid"), has("metrics.demo_inflight_requests")]),
            "server": count([t, has("metrics.system.cpu.load_average.1m")]),
            "log": count([t, term(NS, "fake-typo"), has("attributes.seq")], LOGS)}


def seq_gaps(start, end):
    """這段時間 fake-typo 印的每一行都帶序號。回傳（幾行、漏了幾行、重複幾行）。"""
    doc = es(LOGS + "/_search", {"size": 2000, "_source": ["attributes.seq"], "query": {"bool": {"filter": [
        between(start, end), term(NS, "fake-typo"), has("attributes.seq")]}}})
    seqs = [int(h["_source"]["attributes"]["seq"]) for h in doc["hits"]["hits"]]
    if not seqs:
        return 0, 0, 0
    uniq = set(seqs)
    return len(seqs), max(uniq) - min(uniq) + 1 - len(uniq), len(seqs) - len(uniq)


def wait_es(timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # 查得到東西才算回來：port 通了但資料還沒載入完的時候會回錯誤
            es("metrics-*/_count", {"query": {"match_all": {}}})
            return
        except Exception:
            time.sleep(5)
    raise RuntimeError("Elasticsearch 沒有回來")


def outage():
    print("=== 停機補送：Elasticsearch 停掉，中間重啟收集程式，再恢復 ===")
    end = now() - 10
    base = samples(end - WINDOW, end)
    print("  平常 %d 秒：程式 %d 次、server %d 次、log %d 行" % (WINDOW, base["程式"], base["server"], base["log"]))
    if not all(base.values()):
        print("\nFAIL（停機前就沒有資料，先跑 ./tests/obs/run.sh）")
        return 1
    try:
        kubectl("-n", "obs", "scale", "statefulset/elasticsearch", "--replicas=0")
        kubectl("-n", "obs", "wait", "--for=delete", "pod/elasticsearch-0", "--timeout=180s")
        a = now()
        print("  Elasticsearch 停了，等 %d 秒" % WINDOW)
        time.sleep(WINDOW)
        b = now()
        print("  重啟兩份收集程式（Elasticsearch 還停著）")
        kubectl("-n", "obs", "delete", "pod", "-l", "app in (otel-cluster-collector, otel-node-collector)")
        kubectl("-n", "obs", "rollout", "status", "statefulset/otel-cluster-collector", "--timeout=180s")
        kubectl("-n", "obs", "rollout", "status", "daemonset/otel-node-collector", "--timeout=180s")
        time.sleep(20)
        c = now()
        print("  收集程式回來了，再等 %d 秒" % WINDOW)
        time.sleep(WINDOW)
        d = now()
    finally:
        kubectl("-n", "obs", "scale", "statefulset/elasticsearch", "--replicas=1")
        kubectl("-n", "obs", "rollout", "status", "statefulset/elasticsearch", "--timeout=300s")
        wait_es()
    print("  Elasticsearch 回來了，總共停了 %d 秒。等補送" % (now() - a))
    parts = (("重啟收集程式之前排隊的", a, b), ("重啟收集程式之後排隊的", c, d))
    deadline = time.time() + 240
    while True:
        r = []
        for label, start, stop in parts:
            got = samples(start, stop)
            for kind in base:
                r.append((got[kind] >= NEED * base[kind],
                          "%s：%s 補回 %d / %d" % (label, kind, got[kind], base[kind]),
                          "不到 %d%%" % (NEED * 100)))
        # log 是讀檔案的，連重啟收集程式的那幾十秒也要補得回來，而且不能重複
        total, missing, dup = seq_gaps(a, d)
        r.append((total > 0 and missing == 0, "整段停機的 log 沒有漏（%d 行，序號連續）" % total, "漏了 %d 行" % missing))
        r.append((total > 0 and dup == 0, "整段停機的 log 沒有重複", "重複 %d 行" % dup))
        if all(ok for ok, _, _ in r) or time.time() > deadline:
            return show(r)
        time.sleep(15)


# ---------------------------------------------------------------- restart

def restart():
    print("=== 容器重啟：fake-typo 的 logger 結束後被 K8s 重啟 ===")
    path = "{.items[0].status.containerStatuses[?(@.name=='logger')].restartCount}"

    def restarts():
        return int(kubectl("-n", "fake-typo", "get", "pod", "-l", "app=fake", "-o", "jsonpath=" + path).strip() or 0)

    before = restarts()
    start = now() - 30
    kubectl("-n", "fake-typo", "exec", "deploy/fake", "-c", "logger", "--", "kill", "1")
    deadline = time.time() + 90
    while restarts() == before and time.time() < deadline:
        time.sleep(3)
    after = restarts()
    print("  重啟次數 %d → %d，等 45 秒讓新的容器印一段" % (before, after))
    if after == before:
        print("\nFAIL（容器沒有重啟，這一項等於沒測）")
        return 1
    time.sleep(45)
    doc = es(LOGS + "/_search", {"size": 2000, "query": {"bool": {"filter": [
        between(start, now() + 5), term(NS, "fake-typo"), has("attributes.seq")]}}})
    groups = {}
    for h in doc["hits"]["hits"]:
        src = h["_source"]
        key = str(src["resource"]["attributes"].get("k8s.container.restart_count"))
        groups.setdefault(key, []).append(int(src["attributes"]["seq"]))
    r = []
    old, new = groups.get(str(before), []), groups.get(str(after), [])
    r.append((bool(old) and bool(new), "重啟前、重啟後兩個容器的 log 都收得到（%d 行、%d 行）" % (len(old), len(new)),
              "收到的重啟次數：%s" % sorted(groups)))
    for label, seqs in (("重啟前", old), ("重啟後", new)):
        if not seqs:
            continue
        uniq = set(seqs)
        r.append((max(uniq) - min(uniq) + 1 == len(uniq), "%s的容器：序號 %d 到 %d 連續" % (label, min(uniq), max(uniq)),
                  "漏了 %d 行" % (max(uniq) - min(uniq) + 1 - len(uniq))))
        r.append((len(seqs) == len(uniq), "%s的容器：沒有重複" % label, "重複 %d 行" % (len(seqs) - len(uniq))))
    if new:
        r.append((min(new) == 1, "重啟後的容器從第 1 行開始收", "最小的序號是 %d，前面的沒收到" % min(new)))
    return show(r)


if __name__ == "__main__":
    modes = {"routing": routing, "fault": fault, "outage": outage, "restart": restart}
    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        print(__doc__)
        sys.exit(2)
    sync_clock()
    sys.exit(modes[sys.argv[1]]())

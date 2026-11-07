"""Pod 同儕橫向比對（detector peer、configs/baseline/peer.yaml）的邊緣情境，用合成 metrics 跑真的 ES。

每個情境一個 namespace（pe-…），Pod 名字各不相同，同時寫進同一段時間：
  應用 HTTP 指標 → metrics-generic.otel-default（直方圖 metrics.http.server.request.duration，200／500 各一筆）
  kubeletstats   → metrics-kubeletstatsreceiver.otel-default（k8s.pod.cpu.usage、k8s.pod.memory.working_set）
欄位跟 Collector 寫的一樣（resource.attributes.*、attributes.*），走 elasticsearch-setup.sh 裝的 index 結構。
然後在幾個時間點跑 `detector peer --at …`，看每個情境哪些 Pod 被報，跟預期比，印 PASS／FAIL。

執行（在 engine/ 底下，ES 先跑過 elasticsearch/code/scripts/elasticsearch-setup.sh）：
  ES_URL=http://127.0.0.1:31503 ES_AUTH=x:x python -X utf8 -m tests.peer_edge_e2e
每次跑用新的 namespace（pe<時分>-…），不刪舊資料（慢的機器上 delete_by_query 會逾時），可重跑。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time

import pandas as pd
import requests
import yaml

from interface.cli.detect import main as cli
from common.infra.es import ES
from tests.aiops_es import AiopsES     # engine 自己的資料在 PostgreSQL：aiops-* 的請求轉過去

CONFIG = os.environ.get("PEER_CONFIG", "configs/baseline/peer.yaml")   # 換別的設定比較用（例如改之前的版本）
HTTP_INDEX = "metrics-generic.otel-default"
KUBE_INDEX = "metrics-kubeletstatsreceiver.otel-default"
STEP = 30                                    # 秒；跟 .NET SDK 推送、kubeletstats 收集的間隔一樣
SPAN = 70                                    # 分鐘：寫 T-70m ～ T 的資料
# T：最後一個檢查點。往前取整分鐘，留 2 分鐘：TSDS 只收 now ± 2 小時
T = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=2)).floor("1min")
MB = 1048576
RUN = T.strftime("%H%M")                 # 這次跑的 namespace 前綴：pe<時分>-…，跟上次的資料分開


def full(ns):
    return ns.replace("pe-", f"pe{RUN}-", 1)

# ── Pod 的樣子：t 是相對 T 的分鐘數（負的），born 是 Pod 起來的時間，age = t - born ──
NORMAL = dict(rps=50, err=0.0, lat=0.05, cpu=0.2, mem=300 * MB)


def pod(name, born=-SPAN, died=None, **over):
    """over 的值可以是常數，或 f(t, age) → 值。"""
    return {"name": name, "born": born, "died": died, **{**NORMAL, **over}}


def cold_cpu(steady):        # 起來的前 2 分鐘 JIT、載入類別，1 核
    return lambda t, age: 1.0 if age < 2 else steady


def cold_lat(steady):        # 第一個 30 秒有 10% 的請求很慢（JIT），之後正常
    return lambda t, age: (steady, 0.1, 1.5) if age < 0.5 else steady


def ramp_mem(start, end, minutes):   # 記憶體從 start 慢慢長到 end（JVM heap 長大）
    return lambda t, age: start + (end - start) * min(age / minutes, 1.0)


# 情境：namespace → (說明, 服務名稱→Deployment, Pods)。Deployment 不給就跟服務同名。
def scenarios():
    s = {}
    s["pe-normal3"] = ("3 個 Pod 都正常（本來就有點差）", [
        pod("normal3-a", lat=0.045, cpu=0.18, mem=290 * MB), pod("normal3-b", lat=0.05, cpu=0.2, mem=300 * MB),
        pod("normal3-c", lat=0.06, cpu=0.24, mem=320 * MB)])
    s["pe-onebad3"] = ("3 個 Pod，1 個壞（錯誤率、延遲、CPU、記憶體都高）", [
        pod("onebad3-a"), pod("onebad3-b", err=0.3, lat=2.0, cpu=1.0, mem=600 * MB), pod("onebad3-c")])
    s["pe-two"] = ("只有 2 個 Pod，1 個壞", [
        pod("two-a"), pod("two-b", err=1.0, lat=2.0, cpu=1.0, mem=600 * MB)])
    s["pe-one"] = ("只有 1 個 Pod，壞的", [pod("one-a", err=1.0, lat=2.0, cpu=1.0, mem=600 * MB)])
    s["pe-allbad-eq"] = ("3 個全壞、壞的程度差不多（下游掛了）", [
        pod("allbadeq-a", err=0.95, lat=2.4), pod("allbadeq-b", err=1.0, lat=2.5), pod("allbadeq-c", err=0.97, lat=2.6)])
    s["pe-allbad-uneven"] = ("3 個全壞、其中一個更嚴重", [
        pod("allbadun-a", err=0.40, lat=2.5), pod("allbadun-b", err=0.45, lat=2.6), pod("allbadun-c", err=1.0, lat=8.0)])
    s["pe-twobad3"] = ("3 個 Pod 壞 2 個（多數壞）", [
        pod("twobad3-a"), pod("twobad3-b", err=0.3), pod("twobad3-c", err=0.3)])
    s["pe-twobad5"] = ("5 個 Pod 壞 2 個", [
        pod("twobad5-a"), pod("twobad5-b", err=0.3), pod("twobad5-c"), pod("twobad5-d", err=0.3), pod("twobad5-e")])
    s["pe-small2"] = ("5 個 Pod，兩個各有不同小問題（低於門檻）", [
        pod("small2-a", err=0.03), pod("small2-b", lat=0.07), pod("small2-c"), pod("small2-d"), pod("small2-e")])
    s["pe-diff2"] = ("5 個 Pod，兩個各有不同的問題（一個錯誤率、一個慢）", [
        pod("diff2-a", err=0.2), pod("diff2-b", lat=0.8), pod("diff2-c"), pod("diff2-d"), pod("diff2-e")])
    s["pe-lowvol"] = ("4 個 Pod，1 個流量很少、全部錯誤（10 分鐘 10 個請求）", [
        pod("lowvol-a"), pod("lowvol-b"), pod("lowvol-c"), pod("lowvol-d", rps=lambda t, age: 1 if int(t * 2) % 2 == 0 else 0, err=1.0)])
    # 同名服務、同名 Deployment 在兩個 namespace：各自正常，但兩邊的水準差很多
    s["pe-same-x"] = ("同名服務 order-api 在 namespace X（快）", [
        pod("samex-a", lat=0.05, cpu=0.1), pod("samex-b", lat=0.05, cpu=0.1), pod("samex-c", lat=0.05, cpu=0.1)])
    s["pe-same-y"] = ("同名服務 order-api 在 namespace Y（慢、CPU 高）", [
        pod("samey-a", lat=0.8, cpu=0.6), pod("samey-b", lat=0.8, cpu=0.6), pod("samey-c", lat=0.8, cpu=0.6)])
    # service.name 是 Pod label：穩定版 Deployment 3 個 + 金絲雀 Deployment 1 個（同 label），金絲雀壞了
    s["pe-label"] = ("同一個 label 兩個 Deployment（穩定版 3 + 金絲雀 1，金絲雀錯誤 50%）", [
        pod("label-a"), pod("label-b"), pod("label-c"), pod("canary-a", deploy="order-api-canary", err=0.5)])
    # 新 Pod 剛起來：3 個舊的，T-6 起一個新的（CPU 前 2 分鐘 1 核、第一個 30 秒 10% 請求很慢）
    s["pe-warm"] = ("3 個舊 Pod＋1 個剛起來的新 Pod（暖機）", [
        pod("warm-a"), pod("warm-b"), pod("warm-c"),
        pod("warm-new", born=-8, cpu=cold_cpu(0.2), lat=cold_lat(0.05))])
    # 新 Pod 從一開始就壞（例如那台 node 有問題）：暖機後要抓到
    s["pe-warmbad"] = ("3 個舊 Pod＋1 個剛起來就壞的新 Pod", [
        pod("warmbad-a"), pod("warmbad-b"), pod("warmbad-c"), pod("warmbad-new", born=-8, err=0.5)])
    # Pod 被換掉：b 壞了，T-14 刪掉，同時起一個新名字的 d（正常、有暖機）
    s["pe-replace"] = ("3 個 Pod，壞的那個被換掉（名字變）", [
        pod("replace-a"), pod("replace-b", died=-14, err=0.5, lat=1.5, cpu=1.0),
        pod("replace-c"), pod("replace-d", born=-14, cpu=cold_cpu(0.2), lat=cold_lat(0.05))])
    # 擴縮容 2→5→2：T-40 擴到 5、T-20 縮回 2。像 Java：新 Pod 記憶體 5 分鐘從 200MB 長到 600MB、CPU 前 2 分鐘 1 核
    def scale(prefix, bad_err):
        new = dict(born=-40, died=-20, cpu=cold_cpu(0.2), lat=cold_lat(0.05), mem=ramp_mem(200 * MB, 600 * MB, 5))
        return [pod(f"{prefix}-a", err=bad_err, mem=600 * MB), pod(f"{prefix}-b", mem=600 * MB),
                pod(f"{prefix}-c", **new), pod(f"{prefix}-d", **new), pod(f"{prefix}-e", **new)]
    s["pe-scale"] = ("擴縮容 2→5→2，都正常", scale("scale", 0.0))
    # 2026-10-31 真的 cluster 上的案例：3 個 Pod，c 是 30 分鐘前滾動更新換上的（記憶體 163MB，比舊的 251MB 低），
    # a 洩漏：最近 10 分鐘從 250MB 漲到 420MB。其他 2 個之間差 88MB 把 z 的分母撐大（z 3.3），要靠 z_by_peers 才抓到
    s["pe-ages3"] = ("3 個 Pod 年紀不同（新的記憶體低），一個洩漏", [
        pod("ages3-a", mem=lambda t, age: (250 + 170 * max(0.0, (t + 10) / 10)) * MB),
        pod("ages3-b", mem=251 * MB), pod("ages3-c", born=-30, mem=163 * MB)])
    s["pe-scalebad"] = ("擴縮容 2→5→2，a 一直錯誤 30%", scale("scalebad", 0.3))
    return s


def svc_of(ns):
    return "order-api"


class Maybe:
    def __init__(self, allowed):
        self.allowed = set(allowed)


ALL = ["peer.http_error_rate", "peer.http_latency_p95_s", "peer.pod_cpu", "peer.pod_memory_bytes"]
ER, LAT, CPU, MEM = ALL
HTTP = [ER, LAT]

# 預期：(namespace, 檢查點, {檢查: 要報的 Pod 集合})。沒列的檢查 = 一個都不報。檢查點是相對 T 的分鐘數。
# Maybe({…})：這個時間點報或不報都說得通（寫在 why），但只准報括號裡的 Pod。
EXPECT = [
    ("pe-normal3", 0, {}, "Pod 間本來的差距（p95 45～60ms、CPU 0.18～0.24）不報"),
    ("pe-onebad3", 0, {c: {"onebad3-b"} for c in ALL}, "只報壞的 b，4 個檢查都報"),
    ("pe-two", 0, {}, "2 個分不出誰怪（min_peers 3），peer 不判斷，交給規則／baseline"),
    ("pe-one", 0, {}, "1 個沒得比"),
    ("pe-allbad-eq", 0, {}, "全部一起壞不是某個 Pod 的問題"),
    ("pe-allbad-uneven", 0, {}, "其他 Pod 也壞（中位數 42% 錯誤、p95 2.5 秒）：不能只報一個說它是壞的"),
    ("pe-twobad3", 0, {}, "3 個壞 2 個：其他 Pod 的中位數已經是壞的，分不出"),
    ("pe-twobad5", 0, {ER: {"twobad5-b", "twobad5-d"}}, "5 個壞 2 個：兩個都報"),
    ("pe-small2", 0, {}, "錯誤多 3 個百分點（<5）、慢 1.4 倍（<2 倍）：都低於門檻"),
    ("pe-diff2", 0, {ER: {"diff2-a"}, LAT: {"diff2-b"}}, "各報各的，不互相干擾"),
    ("pe-lowvol", 0, {}, "流量太少（<20 個請求）不比錯誤率、延遲；其他 3 個正常"),
    ("pe-same-x", 0, {}, "兩個 namespace 分開比，不混"),
    ("pe-same-y", 0, {}, "兩個 namespace 分開比，不混"),
    ("pe-label", 0, {ER: {"canary-a"}}, "HTTP 依 service（label）分組：金絲雀跟穩定版一起比、被抓到；資源依 Deployment，金絲雀自己 1 個不比"),
    # 新 Pod 剛起來：T-8 起來
    ("pe-warm", -7, {}, "新 Pod 1 分鐘（CPU 1 核、冷啟動慢）：暖機中不該報"),
    ("pe-warm", -5, {}, "新 Pod 3 分鐘：暖機中不該報"),
    ("pe-warm", 0, {}, "新 Pod 8 分鐘：已正常，不報"),
    ("pe-warmbad", -7, {ER: Maybe({"warmbad-new"})}, "1 分鐘：暖機中，報不報都可以"),
    ("pe-warmbad", 0, {ER: {"warmbad-new"}}, "8 分鐘：一開始就壞的新 Pod 要抓到"),
    # Pod 被換掉：T-14 刪 b、起 d
    ("pe-replace", -16, {ER: {"replace-b"}, LAT: {"replace-b"}, CPU: {"replace-b"}}, "換掉前：報壞的 b"),
    ("pe-replace", -13, {c: Maybe({"replace-b"}) for c in (ER, LAT, CPU)}, "剛換掉 1 分鐘：b 最後的資料還在 fresh（2 分鐘）內，報 b 也合理；新的 d 不能報"),
    ("pe-replace", -11, {}, "換掉 3 分鐘：b 已不在、d 暖機中，都不報"),
    ("pe-replace", -6, {}, "換掉 8 分鐘：d 正常，不報"),
    # 擴縮容：T-40 擴到 5、T-20 縮回 2
    ("pe-ages3", 0, {MEM: {"ages3-a"}}, "3 個 Pod、年紀不同：洩漏的 a（420 vs 251、163MB，z 3.3）要抓到（其他 2 個時 z 門檻 3）；錯誤率、延遲、CPU 都正常不報"),
    ("pe-ages3", -12, {}, "洩漏開始前：a 250、b 251、c 163MB，都不報"),
    ("pe-scale", -41, {}, "2 個：不比"),
    ("pe-scale", -39, {}, "剛擴 1 分鐘：新 Pod CPU 高、記憶體低，都不能報（舊的也不能因為記憶體比新的高被報）"),
    ("pe-scale", -37, {}, "擴 3 分鐘"),
    ("pe-scale", -34, {}, "擴 6 分鐘：新 Pod 已長好"),
    ("pe-scale", -25, {}, "5 個穩定"),
    ("pe-scale", -19, {}, "剛縮 1 分鐘：刪掉的還在 fresh 內"),
    ("pe-scale", -17, {}, "縮 3 分鐘：剩 2 個，不比"),
    ("pe-scalebad", -41, {}, "2 個：不比（a 是壞的也不報）"),
    ("pe-scalebad", -39, {ER: Maybe({"scalebad-a"})}, "剛擴 1 分鐘：新 Pod 暖機中，報不報 a 都可以"),
    ("pe-scalebad", -34, {ER: {"scalebad-a"}}, "擴 6 分鐘：5 個比，抓到 a"),
    ("pe-scalebad", -25, {ER: {"scalebad-a"}}, "5 個穩定：抓到 a"),
    ("pe-scalebad", -17, {}, "縮回 2 個：不比"),
]


# ── 寫資料 ──
def at(p, key, t, age):
    v = p[key]
    return v(t, age) if callable(v) else v


def hist(lat, n):
    """n 個請求的延遲直方圖，p95 ≈ lat。lat 可以是 (正常, 慢的比例, 慢的秒數)。"""
    if isinstance(lat, tuple):
        base, slow_frac, slow = lat
    else:
        base, slow_frac, slow = lat, 0.0, None
    vals = {round(base * 0.4, 6): 0, round(base, 6): 0}
    fast = round(n * (1 - slow_frac))
    vals[round(base * 0.4, 6)] += fast - round(fast * 0.1)
    vals[round(base, 6)] += round(fast * 0.1)
    if slow is not None:
        vals[round(slow, 6)] = vals.get(round(slow, 6), 0) + n - fast
    items = sorted((v, c) for v, c in vals.items() if c > 0)
    return {"values": [v for v, _ in items], "counts": [c for _, c in items]}


def docs_for(ns, p):
    deploy = p.get("deploy", svc_of(ns))
    res = {"k8s.namespace.name": full(ns), "k8s.pod.name": p["name"], "service.name": svc_of(ns),
           "k8s.deployment.name": deploy, "k8s.cluster.name": "peer-synth"}
    out = []
    start = max(p["born"], -SPAN)
    end = p["died"] if p["died"] is not None else 0
    t = start
    while t < end:
        ts = (T + pd.Timedelta(minutes=t)).isoformat()
        age = t - p["born"]
        n = int(at(p, "rps", t, age))
        err = at(p, "err", t, age)
        lat = at(p, "lat", t, age)
        bad = round(n * err)
        for code, cnt in ((200, n - bad), (500, bad)):
            if cnt > 0:
                out.append((HTTP_INDEX, "histogram", "metrics.http.server.request.duration", {
                    "@timestamp": ts, "resource": {"attributes": res},
                    "attributes": {"http.response.status_code": code, "http.request.method": "GET", "http.route": "/orders/{id}"},
                    "metrics": {"http.server.request.duration": hist(lat, cnt)}}))
        out.append((KUBE_INDEX, None, None, {
            "@timestamp": ts, "resource": {"attributes": res},
            "metrics": {"k8s.pod.cpu.usage": at(p, "cpu", t, age), "k8s.pod.memory.working_set": int(at(p, "mem", t, age))}}))
        t += STEP / 60
    return out


def write(es, rows):
    lines = []
    for index, tmpl, field, doc in rows:
        doc = {**doc, "data_stream": {"type": "metrics", "dataset": index.split("-")[1], "namespace": "default"}}
        meta = {"_index": index}
        if tmpl:
            meta["dynamic_templates"] = {field: tmpl}
        else:
            meta["dynamic_templates"] = {"metrics.k8s.pod.cpu.usage": "gauge_double",
                                         "metrics.k8s.pod.memory.working_set": "gauge_long"}
        lines.append(json.dumps({"create": meta}))
        lines.append(json.dumps(doc))
    for i in range(0, len(lines), 1000):
        # 第一次寫要建資料流，慢的機器上會逾時、連線被斷；用 create＋固定時間，重送的會是 409、不會重複
        r = retry(lambda: es.s.post(f"{es.url}/_bulk", data="\n".join(lines[i:i + 1000]) + "\n",
                                    headers={"Content-Type": "application/x-ndjson"}, timeout=300))
        body = r.json()
        if body.get("errors"):
            bad = [it for it in body["items"] if it["create"].get("error") and it["create"]["status"] != 409][:3]
            if bad:
                raise RuntimeError(f"寫入失敗：{bad}")
    retry(lambda: es.req("POST", "/metrics-*.otel-*/_refresh"))


# ── 跑 detector、比對 ──
def retry(fn, tries=5):
    """ES 很忙時連線會逾時、被斷：等一下再試（查詢、create 重送都不會改變結果）。"""
    for k in range(tries):
        try:
            return fn()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, RuntimeError) as e:
            if k == tries - 1:
                raise
            print(f"   （ES 沒回應，10 秒後重試：{str(e)[:80]}）", file=sys.stderr)
            time.sleep(10)


def run_peer(minute):
    with open(CONFIG, encoding="utf-8") as f:
        titles = {c["title"]: c["name"] for c in yaml.safe_load(f)["checks"]}

    def once():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli(["peer", "--config", CONFIG, "--at", (T + pd.Timedelta(minutes=minute)).isoformat()])
        return buf
    buf = retry(once)
    fired = []   # (檢查, namespace, pod, 訊息)
    for line in buf.getvalue().splitlines():
        m = re.match(r"\s+\[\w+\] (.+?)：(pe\d*-[\w-]+)、[^、]+、([\w-]+)（", line)
        if m:
            fired.append((titles[m.group(1)], m.group(2), m.group(3), line.strip()))
    return fired


def main() -> int:
    es = AiopsES(ES())
    print(f"namespace 前綴 pe{RUN}-")
    rows = []
    sc = scenarios()
    for ns, (_, pods) in sc.items():
        for p in pods:
            rows += docs_for(ns, p)
    print(f"T = {T.isoformat()}；寫入 {len(rows)} 筆（{len(sc)} 個情境）")
    write(es, rows)
    fails = 0
    cache = {}
    for ns, minute, want, why in EXPECT:
        if minute not in cache:
            cache[minute] = run_peer(minute)
        fired = [f for f in cache[minute] if f[1] == full(ns)]
        ok, notes = True, []
        for check in ALL:
            got = {f[2] for f in fired if f[0] == check}
            exp = want.get(check, set())
            if isinstance(exp, Maybe):
                if got <= exp.allowed:
                    notes.append(f"{check.split('.')[1]} 報不報都可以（實際 {sorted(got) or '無'}）")
                else:
                    ok = False
                    notes.append(f"{check.split('.')[1]} 只准報 {sorted(exp.allowed)}、實際 {sorted(got)}")
                continue
            if got != exp:
                ok = False
                notes.append(f"{check.split('.')[1]} 預期 {sorted(exp) or '無'}、實際 {sorted(got) or '無'}")
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  [{ns} @T{minute:+d}m] {sc[ns][0]}：{why}")
        for n in notes:
            print(f"        {n}")
        for f in fired:
            print(f"        {f[3]}")
    print()
    print("全部通過" if not fails else f"{fails} 項失敗")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

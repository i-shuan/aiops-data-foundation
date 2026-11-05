"""靜態臨界值規則（configs/baseline/rules.yaml 每一條）的邊緣案例＋ learning loop 規則草稿的動態調整介面，對著一台自己的 ES 跑。

做法：照 Collector 的 elasticsearch 寫入元件在 ES 8.19 存 metrics 的樣子（TSDS 資料流 metrics-<receiver>.otel-p2rules，_source 是
{"resource": {"attributes": …}, "attributes": …, "metrics": {"<名稱>": 值}}，直方圖是 histogram：{"values": […], "counts": […]}，
sum 另存成 <名稱>_sum）
灌合成資料，每個案例一個獨立的對象（Pod、節點、consumer group…），再用真的指令跑：
  python -m detector rules --config configs/baseline/rules.yaml --at <NOW>
從 aiops-anomalies 看每個案例有沒有被對的規則記下來。

會刪掉、重建：metrics-*.otel-p2rules、aiops-anomalies、aiops-tuning（只能對測試用的 ES 跑）。
  ES_URL=http://127.0.0.1:31501 python -X utf8 -m tests.rules_edge_e2e         （在 engine 底下）
  ENGINE_DIR=<另一份 engine>：用那一份的程式與 rules.yaml 跑（重現修之前的行為）
每項印 PASS／FAIL，最後印各類的數字；有 FAIL 時 exit 1。約 2 分鐘。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict

import pandas as pd
import requests

ES_URL = os.environ["ES_URL"].rstrip("/")
AUTH = tuple(os.environ["ES_AUTH"].split(":", 1)) if os.environ.get("ES_AUTH") else None
ENGINE = os.path.abspath(os.environ.get("ENGINE_DIR") or os.path.join(os.path.dirname(__file__), ".."))
CONFIG = "configs/baseline/rules.yaml"
DS = "p2rules"                                      # 資料流的 namespace
NS = "p2r"                                          # K8s namespace（資料裡的）
KC, KL, PR, NTP, APP = (f"metrics-{r}.otel-{DS}" for r in
                        ("k8sclusterreceiver", "kubeletstatsreceiver", "prometheusreceiver", "ntpreceiver", "generic"))
HIST = "histogram"

NOW = pd.Timestamp.now(tz="UTC").floor("1min") + pd.Timedelta(seconds=45)   # 這一輪的時間（--at），slot 是整分
s = requests.Session()
if AUTH:
    s.auth = AUTH

# ── 資料 ──

def ts(sec_ago: float) -> str:
    return (NOW - pd.Timedelta(seconds=sec_ago)).isoformat()


def seq(n: int, last_ago: float = 5, every: float = 30) -> list[float]:
    """n 個點的「幾秒前」，由舊到新；最新一點在 last_ago 秒前。"""
    return [last_ago + every * (n - 1 - i) for i in range(n)]


DOCS: list[tuple[str, dict, dict]] = []             # (index, 文件, dynamic_templates)


def put(index: str, ago: float, res: dict, metrics: dict, attrs: dict | None = None, dyn: dict | None = None):
    # 跟 ES 的 OTLP 入口一樣用 dynamic_templates 指定型別：數值是 double（TSDS 的 _source 是從 doc values 重建的，
    # 不指定會變成 float、49.9 讀回來是 49.900001…），直方圖是 histogram
    dyn = {**{f"metrics.{k}": "gauge_double" for k in metrics}, **(dyn or {})}
    DOCS.append((index, {"@timestamp": ts(ago), "resource": {"attributes": res}, "attributes": attrs or {},
                         "metrics": metrics}, dyn))


def series(index: str, res: dict, vals: list, metric, last_ago: float = 5, every: float = 30, attrs=None, extra=None):
    """vals 由舊到新，每 every 秒一個點；metric 是欄位名稱，或 v -> {欄位: 值} 的函式。None 的點不寫（資料缺漏）。"""
    for ago, v in zip(seq(len(vals), last_ago, every), vals):
        if v is None:
            continue
        m = metric(v) if callable(metric) else {metric: v}
        put(index, ago, res, {**m, **(extra or {})}, attrs)


def pod(name, svc=None, ctr=None, node="p2-node-1"):
    r = {"k8s.namespace.name": NS, "k8s.pod.name": name, "k8s.node.name": node, "service.name": svc or name.rsplit("-", 1)[0]}
    if ctr:
        r["k8s.container.name"] = ctr
    return r


def node(name):
    return {"k8s.node.name": name}


def exp_hist(values: list[float]) -> dict:
    """幾個值 → ES 8.19 的 histogram（{"values": 由小到大, "counts": 各值的次數}；沒有值就是兩個空陣列，ES 收）。"""
    buckets: dict[float, int] = defaultdict(int)
    for v in values:
        buckets[float(v)] += 1
    vals = sorted(buckets)
    return {"values": vals, "counts": [buckets[v] for v in vals]}


# ── 案例：(規則, 說明, 預期觸發?, by 欄位的值) ──
CASES: list[tuple[str, str, bool, dict]] = []
NA = "沒有案例"


def case(rule: str, desc: str, fire: bool, key: dict):
    CASES.append((rule, desc, fire, key))


def K(**kw):
    """by 欄位的值，用短名稱寫。"""
    m = {"ns": "resource.attributes.k8s.namespace.name", "pod": "resource.attributes.k8s.pod.name",
         "ctr": "resource.attributes.k8s.container.name", "node": "resource.attributes.k8s.node.name",
         "svc": "resource.attributes.service.name", "deploy": "resource.attributes.k8s.deployment.name",
         "sts": "resource.attributes.k8s.statefulset.name", "ds": "resource.attributes.k8s.daemonset.name",
         "job": "resource.attributes.k8s.job.name", "method": "attributes.http.request.method",
         "route": "attributes.http.route", "pool": "attributes.jvm_memory_pool_name", "pool_name": "attributes.pool_name",
         "pool.name": "attributes.pool.name", "cg": "attributes.consumergroup", "topic": "attributes.topic",
         "eq": "attributes.equipment_id"}
    return {m[k]: v for k, v in kw.items()}


GONE = 185          # 對象不在了：最後一筆在 3 分多鐘前（fresh 預設 2 分鐘）
N10 = 21            # 10 分多鐘的點數（lookback 10m 之前就有資料）


def build():
    # 1. http.error_rate_floor：window 5m、>= 5%、min_events 20（直方圖：200、500 各一份文件）
    def http(svc, route, ok_counts, err_counts, last_ago=5, every=30, latency=0.05):
        res = {"service.name": svc, "k8s.namespace.name": NS, "k8s.pod.name": f"{svc}-0"}
        for ago, ok, err in zip(seq(len(ok_counts), last_ago, every), ok_counts, err_counts):
            for code, n in ((200, ok), (500, err)):
                if n:
                    put(APP, ago, res, {"http.server.request.duration": exp_hist([latency] * n)},
                        {"http.request.method": "GET", "http.route": route, "http.response.status_code": code},
                        {"metrics.http.server.request.duration": HIST})
    http("p2-err", "/exact", [5, 5, 5, 4], [0, 0, 0, 1])           # 20 個請求錯 1 個 = 5%
    case("http.error_rate_floor", "剛好等於門檻：20 個請求錯 1 個（5%）", True, K(svc="p2-err", method="GET", route="/exact"))
    http("p2-err", "/below", [10, 10, 10, 9], [0, 0, 0, 2])        # 41 個錯 2 個 = 4.9%
    case("http.error_rate_floor", "差一點：41 個錯 2 個（4.9%）", False, K(svc="p2-err", method="GET", route="/below"))
    http("p2-err", "/few", [5, 5, 4], [2, 2, 1])                   # 19 個請求錯 5 個（26%）
    case("http.error_rate_floor", "min_events 不足：19 個請求錯 5 個（26%）", False, K(svc="p2-err", method="GET", route="/few"))
    http("p2-err", "/old", [0, 10, 10, 10, 10], [30, 0, 0, 0, 0], last_ago=5, every=80)   # 錯的那批在 325 秒前
    case("http.error_rate_floor", "lookback 邊界：錯誤都在 5 分 25 秒前", False, K(svc="p2-err", method="GET", route="/old"))

    # 2. http.latency_p95_floor：p95 >= 2 秒、min_events 20
    def lat(route, n, sec):
        res = {"service.name": "p2-lat", "k8s.namespace.name": NS, "k8s.pod.name": "p2-lat-0"}
        for ago in seq(3):
            put(APP, ago, res, {"http.server.request.duration": exp_hist([sec] * n)},
                {"http.request.method": "GET", "http.route": route, "http.response.status_code": 200},
                {"metrics.http.server.request.duration": HIST})
    lat("/slow", 10, 3.0)
    case("http.latency_p95_floor", "30 個請求都 3 秒", True, K(svc="p2-lat", method="GET", route="/slow"))
    lat("/ok", 10, 1.5)
    case("http.latency_p95_floor", "30 個請求都 1.5 秒", False, K(svc="p2-lat", method="GET", route="/ok"))
    lat("/few", 6, 5.0)
    case("http.latency_p95_floor", "min_events 不足：18 個請求 5 秒", False, K(svc="p2-lat", method="GET", route="/few"))

    # 3. container.restarted：increase >= 1（10 分鐘）、report_gone
    def restarts(name, vals, last_ago=5, every=30):
        series(KC, pod(name, svc="p2-crash", ctr="app"), vals, "k8s.container.restarts", last_ago, every)
    restarts("p2-crash-a", [0] * 10 + [1])
    case("container.restarted", "重啟 1 次（剛好等於門檻），context 帶 Pod label 的服務名稱", True,
         K(ns=NS, pod="p2-crash-a", ctr="app"))
    restarts("p2-crash-b", [3] * 12)
    case("container.restarted", "次數一直是 3", False, K(ns=NS, pod="p2-crash-b", ctr="app"))
    restarts("p2-crash-gone", [0, 0, 1], last_ago=300)
    case("container.restarted", "Pod 已刪除（最後一筆 5 分鐘前）：report_gone 照報", True, K(ns=NS, pod="p2-crash-gone", ctr="app"))
    restarts("p2-crash-old", [0, 0] + [1] * 22)                      # 1 在 11 分 35 秒前就有了
    case("container.restarted", "lookback 邊界：重啟在 10 分鐘以前", False, K(ns=NS, pod="p2-crash-old", ctr="app"))
    restarts("p2-crash-reset", [5, 6, 0, 1])
    case("container.restarted", "計數歸零重來（5→6→0→1 算 2 次）", True, K(ns=NS, pod="p2-crash-reset", ctr="app"))
    restarts("p2-crash-one", [7])
    case("container.restarted", "資料缺漏：只有一個點", False, K(ns=NS, pod="p2-crash-one", ctr="app"))

    # 4～6. 副本不夠：compare desired − available >= 1、for 3m、min_ratio 0.8（for 裡有 6 個點，要 5 個）
    def replicas(kind, name, avail, desired=2, last_ago=5):
        f = {"deployment": ("k8s.deployment.available", "k8s.deployment.desired"),
             "statefulset": ("k8s.statefulset.ready_pods", "k8s.statefulset.desired_pods"),
             "daemonset": ("k8s.daemonset.ready_nodes", "k8s.daemonset.desired_scheduled_nodes")}[kind]
        res = {"k8s.namespace.name": NS, f"k8s.{kind}.name": name, "service.name": name}
        series(KC, res, avail, lambda v: {f[0]: v, f[1]: desired}, last_ago)
    D = "deployment.unavailable"
    replicas("deployment", "p2-dep-hold", [2] * 15 + [0] * 6)
    case(D, "for 剛好：3 分鐘裡 6/6 不夠", True, K(ns=NS, deploy="p2-dep-hold"))
    replicas("deployment", "p2-dep-flap", [2] * 15 + [0, 0, 2, 0, 0, 0])
    case(D, "中斷又恢復：6 點裡 5 點不夠（83%）", True, K(ns=NS, deploy="p2-dep-flap"))
    replicas("deployment", "p2-dep-flap2", [2] * 15 + [0, 2, 0, 2, 0, 0])
    case(D, "中斷又恢復：6 點裡 4 點不夠（67%）", False, K(ns=NS, deploy="p2-dep-flap2"))
    replicas("deployment", "p2-dep-back", [2] * 15 + [0] * 5 + [2])
    case(D, "最後一點已恢復", False, K(ns=NS, deploy="p2-dep-back"))
    replicas("deployment", "p2-dep-dip", [2] * 19 + [1, 1])
    case(D, "換版短暫不足（最後 1 分鐘）", False, K(ns=NS, deploy="p2-dep-dip"))
    replicas("deployment", "p2-dep-new", [0] * 6)
    case(D, "新建立 2.5 分鐘、從第一筆就不夠（for 3m 還沒到）", False, K(ns=NS, deploy="p2-dep-new"))
    replicas("deployment", "p2-dep-new7", [0] * 7)
    case(D, "新建立 3 分 5 秒、一直不夠（剛好蓋滿 for）", True, K(ns=NS, deploy="p2-dep-new7"))
    replicas("deployment", "p2-dep-zero", [0] * N10, desired=0)
    case(D, "縮到 0（desired 0、available 0）", False, K(ns=NS, deploy="p2-dep-zero"))
    replicas("deployment", "p2-dep-gone", [0] * 15, last_ago=GONE)
    case(D, "Deployment 已刪除（最後一筆 3 分鐘前）", False, K(ns=NS, deploy="p2-dep-gone"))
    replicas("deployment", "p2-dep-gap", [2] * 14 + [0, None, None, None, None, 0])
    case(D, "資料缺漏：中間缺 2 分鐘，頭尾兩點都不夠（只看有資料的點）", True, K(ns=NS, deploy="p2-dep-gap"))
    replicas("deployment", "p2-dep-gap2", [2] * 14 + [None] * 4 + [0, 0])
    case(D, "資料缺漏：缺了一段後只有最後 2 點（不到 for 一半）", False, K(ns=NS, deploy="p2-dep-gap2"))
    for kind, rule in (("statefulset", "statefulset.unavailable"), ("daemonset", "daemonset.unavailable")):
        kk = {"statefulset": "sts", "daemonset": "ds"}[kind]
        replicas(kind, f"p2-{kk}-hold", [2] * 15 + [1] * 6)
        case(rule, "for 剛好：3 分鐘 6/6 少 1 個（剛好等於門檻）", True, K(ns=NS, **{kk: f"p2-{kk}-hold"}))
        replicas(kind, f"p2-{kk}-dip", [2] * 19 + [1, 1])
        case(rule, "短暫不足（最後 1 分鐘）", False, K(ns=NS, **{kk: f"p2-{kk}-dip"}))
        replicas(kind, f"p2-{kk}-gone", [1] * 15, last_ago=GONE)
        case(rule, "已刪除（最後一筆 3 分鐘前）", False, K(ns=NS, **{kk: f"p2-{kk}-gone"}))

    # 7. pod.failed：phase == 4（最新一點）
    series(KC, pod("p2-fail-a"), [2] * 5 + [4], "k8s.pod.phase")
    case("pod.failed", "phase 4（Failed）", True, K(ns=NS, pod="p2-fail-a"))
    series(KC, pod("p2-fail-b"), [2] * 5 + [3], "k8s.pod.phase")
    case("pod.failed", "phase 3（Succeeded）", False, K(ns=NS, pod="p2-fail-b"))
    series(KC, pod("p2-fail-late"), [2, 4, 4], "k8s.pod.phase", last_ago=95)
    case("pod.failed", "最後一筆 95 秒前（fresh 2 分鐘內）", True, K(ns=NS, pod="p2-fail-late"))
    series(KC, pod("p2-fail-gone"), [2, 4, 4], "k8s.pod.phase", last_ago=GONE)
    case("pod.failed", "Pod 已刪除（最後一筆 3 分鐘前）", False, K(ns=NS, pod="p2-fail-gone"))

    # 8. pod.pending：phase == 1、for 5m
    series(KC, pod("p2-pend-hold"), [1] * 11, "k8s.pod.phase")          # 從 5 分 5 秒前就 Pending
    case("pod.pending", "新 Pod Pending 5 分 5 秒（剛好蓋滿 for）", True, K(ns=NS, pod="p2-pend-hold"))
    series(KC, pod("p2-pend-new"), [1] * 9, "k8s.pod.phase")            # 4 分 5 秒
    case("pod.pending", "新 Pod Pending 4 分 5 秒（for 5m 還沒到）", False, K(ns=NS, pod="p2-pend-new"))
    series(KC, pod("p2-pend-run"), [1] * 10 + [2], "k8s.pod.phase")
    case("pod.pending", "Pending 後變 Running", False, K(ns=NS, pod="p2-pend-run"))
    series(KC, pod("p2-pend-flap"), [1] * 6 + [2] + [1] * 4, "k8s.pod.phase")
    case("pod.pending", "中斷又恢復（for 裡一點 Running，min_ratio 1）", False, K(ns=NS, pod="p2-pend-flap"))
    series(KC, pod("p2-pend-gone"), [1] * 11, "k8s.pod.phase", last_ago=GONE)
    case("pod.pending", "Pod 已刪除", False, K(ns=NS, pod="p2-pend-gone"))

    # 9. job.failed：increase >= 1、report_gone
    def job(name, vals, last_ago=5):
        series(KC, {"k8s.namespace.name": NS, "k8s.job.name": name}, vals, "k8s.job.failed_pods", last_ago)
    job("p2-job-a", [0] * 8 + [1])
    case("job.failed", "失敗的 Pod 0 → 1", True, K(ns=NS, job="p2-job-a"))
    job("p2-job-b", [0] * 9)
    case("job.failed", "一直 0", False, K(ns=NS, job="p2-job-b"))
    job("p2-job-gone", [0, 0, 1], last_ago=240)
    case("job.failed", "Job 已刪除（最後一筆 4 分鐘前）：report_gone 照報", True, K(ns=NS, job="p2-job-gone"))

    # 10. node.not_ready：ready == 0、for 1m（for 裡 2 點）
    series(KC, node("p2-nr-a"), [1] * 10 + [0, 0], "k8s.node.condition_ready")
    case("node.not_ready", "for 剛好：最後 2 點 NotReady", True, K(node="p2-nr-a"))
    series(KC, node("p2-nr-b"), [1] * 10 + [0], "k8s.node.condition_ready")
    case("node.not_ready", "只有最後 1 點 NotReady", False, K(node="p2-nr-b"))
    series(KC, node("p2-nr-gone"), [0] * 6, "k8s.node.condition_ready", last_ago=GONE)
    case("node.not_ready", "節點已拿掉（最後一筆 3 分鐘前）", False, K(node="p2-nr-gone"))

    # 11、12. 主機時鐘偏移：abs >= 100ms／1 秒，lookback 40m、fresh 40m（30 分鐘量一次）
    def ntp(name, pts):
        for ago, ns_ in pts:
            put(NTP, ago, node(name), {"ntp.offset": ns_})
    ntp("p2-ntp-exact", [(600, 100_000_000)])
    case("node.clock_offset", "剛好 100ms", True, K(node="p2-ntp-exact"))
    ntp("p2-ntp-below", [(600, 99_999_999)])
    case("node.clock_offset", "差一點：99.999999ms", False, K(node="p2-ntp-below"))
    ntp("p2-ntp-neg", [(600, -150_000_000)])
    case("node.clock_offset", "慢 150ms（負的，abs）", True, K(node="p2-ntp-neg"))
    ntp("p2-ntp-39m", [(39 * 60, 200_000_000)])
    case("node.clock_offset", "39 分鐘前量的（fresh 40m 內）", True, K(node="p2-ntp-39m"))
    ntp("p2-ntp-41m", [(41 * 60, 200_000_000)])
    case("node.clock_offset", "41 分鐘前量的（lookback 外）", False, K(node="p2-ntp-41m"))
    ntp("p2-ntp-fixed", [(35 * 60, 300_000_000), (5 * 60, 10_000_000)])
    case("node.clock_offset", "之前偏、最新一次正常", False, K(node="p2-ntp-fixed"))
    ntp("p2-ntpc-exact", [(600, 1_000_000_000)])
    case("node.clock_offset_critical", "剛好 1 秒", True, K(node="p2-ntpc-exact"))
    ntp("p2-ntpc-below", [(600, 999_000_000)])
    case("node.clock_offset_critical", "999ms", False, K(node="p2-ntpc-below"))
    case("node.clock_offset", "999ms 由非 critical 那條報", True, K(node="p2-ntpc-below"))
    ntp("p2-ntpc-41m", [(41 * 60, -2_000_000_000)])
    case("node.clock_offset_critical", "41 分鐘前量的", False, K(node="p2-ntpc-41m"))

    # 13、14. 節點壓力：== 1（最新一點）
    for f, rule in (("k8s.node.condition_memory_pressure", "node.memory_pressure"),
                    ("k8s.node.condition_disk_pressure", "node.disk_pressure")):
        short = rule.split(".")[1].split("_")[0]
        series(KC, node(f"p2-{short}-a"), [0] * 3 + [1], f)
        case(rule, "最新一點 1", True, K(node=f"p2-{short}-a"))
        series(KC, node(f"p2-{short}-b"), [1] * 3 + [0], f)
        case(rule, "最新一點 0（已恢復）", False, K(node=f"p2-{short}-b"))
        series(KC, node(f"p2-{short}-gone"), [1] * 3, f, last_ago=GONE)
        case(rule, "節點已拿掉", False, K(node=f"p2-{short}-gone"))

    # 15. scrape.down：up == 0、for 1m
    series(PR, pod("p2-up-a"), [1] * 10 + [0, 0], "up")
    case("scrape.down", "for 剛好：最後 2 點 up=0", True, K(ns=NS, pod="p2-up-a"))
    series(PR, pod("p2-up-b"), [1] * 10 + [0], "up")
    case("scrape.down", "只有最後 1 點 up=0", False, K(ns=NS, pod="p2-up-b"))
    series(PR, pod("p2-up-gone"), [1] * 5 + [0, 0, 0], "up", last_ago=GONE)
    case("scrape.down", "Pod 已刪除（最後 up=0，3 分鐘前）", False, K(ns=NS, pod="p2-up-gone"))

    # 16. container.memory_near_limit：ws ÷ (ws + available) >= 0.9、for 2m（for 裡 4 點）
    def mem(name, pairs, last_ago=5):
        series(KL, pod(name, svc="p2-mem", ctr="app"), pairs,
               lambda p: {"container.memory.working_set": p[0], **({"container.memory.available": p[1]} if p[1] is not None else {})},
               last_ago)
    mem("p2-mem-exact", [(500, 500)] * 10 + [(900, 100)] * 5)
    case("container.memory_near_limit", "剛好 90%、蓋滿 2 分鐘", True, K(ns=NS, pod="p2-mem-exact", ctr="app"))
    mem("p2-mem-below", [(8999, 1001)] * 15)
    case("container.memory_near_limit", "差一點：89.99%", False, K(ns=NS, pod="p2-mem-below", ctr="app"))
    mem("p2-mem-nolimit", [(64_000_000, None)] * 15)
    case("container.memory_near_limit", "資料缺漏：沒設上限（沒有 available）", False, K(ns=NS, pod="p2-mem-nolimit", ctr="app"))
    mem("p2-mem-flap", [(950, 50)] * 12 + [(850, 150)] + [(950, 50)] * 2)
    case("container.memory_near_limit", "中斷又恢復：for 裡一點 85%", False, K(ns=NS, pod="p2-mem-flap", ctr="app"))
    mem("p2-mem-gone", [(950, 50)] * 10, last_ago=GONE)
    case("container.memory_near_limit", "Pod 已刪除", False, K(ns=NS, pod="p2-mem-gone", ctr="app"))
    mem("p2-mem-new", [(950, 50)] * 4)
    case("container.memory_near_limit", "新容器 1 分 35 秒、一直 95%（for 2m 還沒到）", False, K(ns=NS, pod="p2-mem-new", ctr="app"))

    # 16b. container.memory_leak：最近 30 分鐘的 Theil–Sen 斜率、三段低點墊高、<= 6 小時到上限（ws + available）；61 點
    N30 = 61
    leak = lambda ws, limit=1000: [(w, limit - w) for w in ws]
    mem("p2-leak-linear", leak([500 + 5 * i for i in range(N30)]))
    case("container.memory_leak", "30 分鐘 500 → 800（每小時 600）、上限 1000：0.33 小時後到", True, K(ns=NS, pod="p2-leak-linear", ctr="app"))
    mem("p2-leak-saw", leak([500 + 5 * i + 20 * (i % 10) for i in range(N30)]))
    case("container.memory_leak", "GC 鋸齒但每次 GC 後的低點都更高", True, K(ns=NS, pod="p2-leak-saw", ctr="app"))
    mem("p2-leak-gc", leak([500 + 20 * (i % 10) for i in range(N30)]))
    case("container.memory_leak", "純 GC 鋸齒（低點不變）", False, K(ns=NS, pod="p2-leak-gc", ctr="app"))
    mem("p2-leak-far", leak([500 + 5 * i for i in range(N30)], limit=100_000))
    case("container.memory_leak", "一樣的速度但上限很遠（165 小時）", False, K(ns=NS, pod="p2-leak-far", ctr="app"))
    mem("p2-leak-new", leak([500 + 5 * i for i in range(N10)]))
    case("container.memory_leak", "新容器只有 10 分鐘資料（啟動期 heap 在長）", False, K(ns=NS, pod="p2-leak-new", ctr="app"))
    mem("p2-leak-nolimit", [(500 + 5 * i, None) for i in range(N30)])
    case("container.memory_leak", "沒設上限（沒有 available）", False, K(ns=NS, pod="p2-leak-nolimit", ctr="app"))
    mem("p2-leak-spike", leak([500] * 30 + [950] + [500] * 30))
    case("container.memory_leak", "中間一點飆高又回來", False, K(ns=NS, pod="p2-leak-spike", ctr="app"))
    mem("p2-leak-gone", leak([500 + 5 * i for i in range(N30)]), last_ago=GONE)
    case("container.memory_leak", "Pod 已刪除", False, K(ns=NS, pod="p2-leak-gone", ctr="app"))

    # 17～19. 節點記憶體、磁碟（ratio、for 5m／5m／1m）
    series(KL, node("p2-nmem-a"), [(90, 10)] * N10,
           lambda p: {"k8s.node.memory.working_set": p[0], "k8s.node.memory.available": p[1]})
    case("node.memory_high", "剛好 90% 持續 10 分鐘", True, K(node="p2-nmem-a"))
    series(KL, node("p2-nmem-b"), [(89, 11)] * N10,
           lambda p: {"k8s.node.memory.working_set": p[0], "k8s.node.memory.available": p[1]})
    case("node.memory_high", "89%", False, K(node="p2-nmem-b"))
    series(KL, node("p2-nmem-gone"), [(95, 5)] * N10,
           lambda p: {"k8s.node.memory.working_set": p[0], "k8s.node.memory.available": p[1]}, last_ago=GONE)
    case("node.memory_high", "節點已拿掉", False, K(node="p2-nmem-gone"))
    disk = lambda u: {"k8s.node.filesystem.usage": u, "k8s.node.filesystem.capacity": 100}
    series(KL, node("p2-disk-85"), [50] * 10 + [85] * 11, disk)
    case("node.disk_high", "剛好 85%、5 分 5 秒", True, K(node="p2-disk-85"))
    series(KL, node("p2-disk-84"), [84] * N10, disk)
    case("node.disk_high", "84%", False, K(node="p2-disk-84"))
    series(KL, node("p2-disk-short"), [50] * 12 + [90] * 9, disk)
    case("node.disk_high", "90% 只有 4 分 5 秒", False, K(node="p2-disk-short"))
    series(KL, node("p2-disk-gone"), [90] * N10, disk, last_ago=GONE)
    case("node.disk_high", "節點已拿掉", False, K(node="p2-disk-gone"))
    series(KL, node("p2-full-a"), [50] * 10 + [95, 95, 95], disk)
    case("node.disk_full", "剛好 95%、for 1m", True, K(node="p2-full-a"))
    series(KL, node("p2-full-b"), [50] * 10 + [94.9] * 3, disk)
    case("node.disk_full", "94.9%", False, K(node="p2-full-b"))
    series(KL, node("p2-full-one"), [50] * 10 + [99], disk)
    case("node.disk_full", "只有最後 1 點 99%", False, K(node="p2-full-one"))

    # 20. container.cpu_throttled：increase_ratio >= 25%、for 5m（cAdvisor 累計值）
    def thr(name, per_step, thr_step, n=N10, last_ago=5, reset_at=None):
        p = t = 0
        vals = []
        for i in range(n):
            if reset_at is not None and i == reset_at:
                p = t = 0
            vals.append((p, t))
            p, t = p + per_step, t + thr_step
        series(PR, pod(name, svc="p2-thr", ctr="app"), vals,
               lambda v: {"container_cpu_cfs_periods_total": v[0], "container_cpu_cfs_throttled_periods_total": v[1]}, last_ago)
    thr("p2-thr-exact", 100, 25)
    case("container.cpu_throttled", "剛好 25% 的週期被限流", True, K(ns=NS, pod="p2-thr-exact", ctr="app"))
    thr("p2-thr-below", 100, 24)
    case("container.cpu_throttled", "24%", False, K(ns=NS, pod="p2-thr-below", ctr="app"))
    thr("p2-thr-reset", 100, 50, reset_at=17)
    case("container.cpu_throttled", "計數歸零重來（容器重建）仍是 50%", True, K(ns=NS, pod="p2-thr-reset", ctr="app"))
    thr("p2-thr-gone", 100, 50, n=10, last_ago=GONE)
    case("container.cpu_throttled", "Pod 已刪除", False, K(ns=NS, pod="p2-thr-gone", ctr="app"))
    thr("p2-thr-one", 100, 50, n=1)
    case("container.cpu_throttled", "資料缺漏：只有一個點", False, K(ns=NS, pod="p2-thr-one", ctr="app"))

    # 21. jvm.old_gen_after_gc_high：ratio >= 0.8、for 2m、只看 Old／Tenured
    def jvm(name, pool, ratio, n=N10, last_ago=5):
        series(APP, pod(name, svc="p2-java"), [ratio] * n,
               lambda r: {"jvm_memory_used_after_last_gc_bytes": r * 1000, "jvm_memory_limit_bytes": 1000},
               last_ago, attrs={"jvm_memory_pool_name": pool, "jvm_memory_type": "heap"})
    jvm("p2-jvm-old", "G1 Old Gen", 0.8)
    case("jvm.old_gen_after_gc_high", "G1 Old Gen 剛好 80%", True, K(ns=NS, pod="p2-jvm-old", pool="G1 Old Gen"))
    jvm("p2-jvm-old79", "G1 Old Gen", 0.79)
    case("jvm.old_gen_after_gc_high", "79%", False, K(ns=NS, pod="p2-jvm-old79", pool="G1 Old Gen"))
    jvm("p2-jvm-eden", "G1 Eden Space", 0.95)
    case("jvm.old_gen_after_gc_high", "Eden 95%（filter 只看老年代）", False, K(ns=NS, pod="p2-jvm-eden", pool="G1 Eden Space"))
    jvm("p2-jvm-ten", "Tenured Gen", 0.9)
    case("jvm.old_gen_after_gc_high", "Tenured Gen 90%（Serial GC 的名稱）", True, K(ns=NS, pod="p2-jvm-ten", pool="Tenured Gen"))
    jvm("p2-jvm-gone", "G1 Old Gen", 0.95, n=10, last_ago=GONE)
    case("jvm.old_gen_after_gc_high", "Pod 已刪除", False, K(ns=NS, pod="p2-jvm-gone", pool="G1 Old Gen"))

    # 22、23. GC 次數、GC 時間比例（直方圖，每 30 秒一份差值；sum 由 Collector 另存成 jvm_gc_duration_seconds_sum）
    def gc(name, action, per_point: list, sec_each=0.07, last_ago=5):
        for ago, n in zip(seq(len(per_point), last_ago), per_point):
            put(APP, ago, pod(name, svc="p2-java"),
                {"jvm_gc_duration_seconds": exp_hist([sec_each] * n), "jvm_gc_duration_seconds_sum": sec_each * n},
                {"jvm_gc_action": action, "jvm_gc_name": "G1 Old Generation" if "major" in action else "G1 Young Generation"},
                {"metrics.jvm_gc_duration_seconds": HIST})
    gc("p2-gc-5", "end of major GC", [0] * 11 + [1, 0, 1, 0, 1, 0, 1, 0, 1, 0])
    case("jvm.major_gc_frequent", "5 分鐘剛好 5 次", True, K(ns=NS, pod="p2-gc-5"))
    gc("p2-gc-4", "end of major GC", [0] * 8 + [3, 0, 0] + [1, 0, 1, 0, 1, 0, 1, 0, 0, 0])
    case("jvm.major_gc_frequent", "5 分鐘 4 次（之前那 3 次在 for 外）", False, K(ns=NS, pod="p2-gc-4"))
    gc("p2-gc-minor", "end of minor GC", [2] * N10)
    case("jvm.major_gc_frequent", "minor GC 42 次（filter）", False, K(ns=NS, pod="p2-gc-minor"))
    gc("p2-gc-gone", "end of major GC", [2] * 10, last_ago=GONE)
    case("jvm.major_gc_frequent", "Pod 已刪除", False, K(ns=NS, pod="p2-gc-gone"))
    # GC 時間比例：for 3m 裡 6 點、跨 150+30 秒；每點 3 秒 → 18/180 = 10%
    gc("p2-gco-10", "end of minor GC", [0] * 15 + [1] * 6, sec_each=3.0)
    case("jvm.gc_overhead", "剛好 10%（每 30 秒 GC 3 秒）", True, K(ns=NS, pod="p2-gco-10"))
    gc("p2-gco-9", "end of minor GC", [1] * N10, sec_each=2.9)
    case("jvm.gc_overhead", "9.7%", False, K(ns=NS, pod="p2-gco-9"))
    gc("p2-gco-gone", "end of minor GC", [1] * 10, sec_each=6.0, last_ago=GONE)
    case("jvm.gc_overhead", "Pod 已刪除", False, K(ns=NS, pod="p2-gco-gone"))

    # 24. db.pool_exhausted：used ÷ max（max 在另一份文件，join）、for 1m
    def pool(name, used, mx=10, n=N10, last_ago=5, with_max=True, idle=0, otlp=False):
        res = pod(name, svc="p2-db")
        pn = "pool.name" if otlp else "pool_name"
        pre = "db.client.connections." if otlp else "db_client_connections_"
        for ago in seq(n, last_ago):
            put(APP, ago, res, {pre + "usage": used}, {pn: "HikariPool-1", "state": "used"})
            put(APP, ago, res, {pre + "usage": idle}, {pn: "HikariPool-1", "state": "idle"})
            if with_max:
                put(APP, ago, res, {pre + "max": mx}, {pn: "HikariPool-1"})
    pool("p2-pool-9", 9)
    case("db.pool_exhausted", "used 9／max 10（剛好 90%）", True, K(ns=NS, pod="p2-pool-9", pool_name="HikariPool-1"))
    pool("p2-pool-8", 8)
    case("db.pool_exhausted", "used 8／10", False, K(ns=NS, pod="p2-pool-8", pool_name="HikariPool-1"))
    pool("p2-pool-idle", 1, idle=10)
    case("db.pool_exhausted", "idle 10、used 1（filter state=used）", False, K(ns=NS, pod="p2-pool-idle", pool_name="HikariPool-1"))
    pool("p2-pool-nomax", 10, with_max=False)
    case("db.pool_exhausted", "資料缺漏：沒有 max（join 補不到）", False, K(ns=NS, pod="p2-pool-nomax", pool_name="HikariPool-1"))
    pool("p2-pool-gone", 10, n=10, last_ago=GONE)
    case("db.pool_exhausted", "Pod 已刪除", False, K(ns=NS, pod="p2-pool-gone", pool_name="HikariPool-1"))
    pool("p2-poolo-9", 9, otlp=True)
    case("db.pool_exhausted_otlp", "used 9／max 10（剛好 90%）", True, K(ns=NS, pod="p2-poolo-9", **{"pool.name": "HikariPool-1"}))
    pool("p2-poolo-8", 8.9, otlp=True)
    case("db.pool_exhausted_otlp", "89%", False, K(ns=NS, pod="p2-poolo-8", **{"pool.name": "HikariPool-1"}))
    pool("p2-poolo-gone", 10, n=10, last_ago=GONE, otlp=True)
    case("db.pool_exhausted_otlp", "Pod 已刪除", False, K(ns=NS, pod="p2-poolo-gone", **{"pool.name": "HikariPool-1"}))

    # 25. db.pool_waiting：pending >= 1、for 1m、min_ratio 0.8
    def wait(name, vals, last_ago=5):
        series(APP, pod(name, svc="p2-db"), vals, "db_client_connections_pending_requests", last_ago, attrs={"pool_name": "HikariPool-1"})
    wait("p2-wait-1", [0] * 10 + [1, 1, 1])
    case("db.pool_waiting", "剛好 1 個在排、1 分鐘", True, K(ns=NS, pod="p2-wait-1", pool_name="HikariPool-1"))
    wait("p2-wait-0", [0] * 13)
    case("db.pool_waiting", "0", False, K(ns=NS, pod="p2-wait-0", pool_name="HikariPool-1"))
    wait("p2-wait-gone", [3] * 10, last_ago=GONE)
    case("db.pool_waiting", "Pod 已刪除", False, K(ns=NS, pod="p2-wait-gone", pool_name="HikariPool-1"))

    # 26～33. .NET（9 以後 dotnet.*、8 以前 process.runtime.dotnet.*）
    def counter(name, field, start, steps: list, attrs=None, last_ago=5):
        v, vals = start, []
        for d in [0] + steps:
            v += d
            vals.append(v)
        series(APP, pod(name, svc="p2-net"), vals, field, last_ago, attrs=attrs)
    # 計數器每 30 秒加多少；for 5m 裡有 10 點 → 9 個差
    counter("p2-g2-5", "dotnet.gc.collections", 100, [0] * 11 + [1, 0, 1, 0, 1, 0, 1, 0, 1], {"gc.heap.generation": "gen2"})
    case("dotnet.gen2_gc_frequent", "5 分鐘剛好 +5", True, K(ns=NS, pod="p2-g2-5"))
    counter("p2-g2-4", "dotnet.gc.collections", 100, [0] * 8 + [5, 5, 0] + [1, 0, 1, 0, 1, 0, 1, 0, 0], {"gc.heap.generation": "gen2"})
    case("dotnet.gen2_gc_frequent", "5 分鐘 +4（之前 +10 在 for 外）", False, K(ns=NS, pod="p2-g2-4"))
    counter("p2-g2-gen0", "dotnet.gc.collections", 0, [10] * 20, {"gc.heap.generation": "gen0"})
    case("dotnet.gen2_gc_frequent", "gen0 +200（filter）", False, K(ns=NS, pod="p2-g2-gen0"))
    counter("p2-g2-gone", "dotnet.gc.collections", 0, [5] * 9, {"gc.heap.generation": "gen2"}, last_ago=GONE)
    case("dotnet.gen2_gc_frequent", "Pod 已刪除（沒有 report_gone）", False, K(ns=NS, pod="p2-g2-gone"))
    counter("p2-oom-1", "dotnet.exceptions", 0, [0] * 19 + [1], {"error.type": "OutOfMemoryException"})
    case("dotnet.out_of_memory", "OutOfMemoryException +1", True, K(ns=NS, pod="p2-oom-1"))
    counter("p2-oom-other", "dotnet.exceptions", 0, [50] * 20, {"error.type": "InvalidOperationException"})
    case("dotnet.out_of_memory", "別種例外 +1000（filter）", False, K(ns=NS, pod="p2-oom-other"))
    counter("p2-oom-gone", "dotnet.exceptions", 0, [1] * 9, {"error.type": "OutOfMemoryException"}, last_ago=GONE)
    case("dotnet.out_of_memory", "Pod 已刪除（沒有 report_gone）", False, K(ns=NS, pod="p2-oom-gone"))
    for pre, rule in (("dotnet.thread_pool.queue.length", "dotnet.thread_pool_starvation"),
                      ("process.runtime.dotnet.thread_pool.queue.length", "dotnet8.thread_pool_starvation")):
        tag = "q9" if rule.startswith("dotnet.") else "q8"
        series(APP, pod(f"p2-{tag}-10", svc="p2-net"), [0] * 10 + [10] * 11, pre)
        case(rule, "剛好 10、蓋滿 2 分鐘", True, K(ns=NS, pod=f"p2-{tag}-10"))
        series(APP, pod(f"p2-{tag}-9", svc="p2-net"), [9] * N10, pre)
        case(rule, "9", False, K(ns=NS, pod=f"p2-{tag}-9"))
        series(APP, pod(f"p2-{tag}-gone", svc="p2-net"), [50] * 10, pre, last_ago=GONE)
        case(rule, "Pod 已刪除", False, K(ns=NS, pod=f"p2-{tag}-gone"))
    for thr_f, cpu_f, rule in (("dotnet.thread_pool.thread.count", "dotnet.process.cpu.count", "dotnet.thread_pool_growth"),
                               ("process.runtime.dotnet.thread_pool.threads.count", "process.cpu.count", "dotnet8.thread_pool_growth")):
        tag = "t9" if rule.startswith("dotnet.") else "t8"
        series(APP, pod(f"p2-{tag}-16", svc="p2-net"), [6] * 10 + [32] * 11, lambda v: {thr_f: v, cpu_f: 2})
        case(rule, "32 條／2 核（剛好 16）", True, K(ns=NS, pod=f"p2-{tag}-16"))
        series(APP, pod(f"p2-{tag}-15", svc="p2-net"), [30] * N10, lambda v: {thr_f: v, cpu_f: 2})
        case(rule, "30 條／2 核（15）", False, K(ns=NS, pod=f"p2-{tag}-15"))
        series(APP, pod(f"p2-{tag}-nocpu", svc="p2-net"), [100] * N10, thr_f)
        case(rule, "資料缺漏：沒有 CPU 核數", False, K(ns=NS, pod=f"p2-{tag}-nocpu"))
        series(APP, pod(f"p2-{tag}-gone", svc="p2-net"), [100] * 10, lambda v: {thr_f: v, cpu_f: 2}, last_ago=GONE)
        case(rule, "Pod 已刪除", False, K(ns=NS, pod=f"p2-{tag}-gone"))
    counter("p2-g8-5", "process.runtime.dotnet.gc.collections.count", 10, [0] * 11 + [1, 0, 1, 0, 1, 0, 1, 0, 1], {"generation": "gen2"})
    case("dotnet8.gen2_gc_frequent", "5 分鐘剛好 +5", True, K(ns=NS, pod="p2-g8-5"))
    counter("p2-g8-4", "process.runtime.dotnet.gc.collections.count", 10, [0] * 11 + [1, 0, 1, 0, 1, 0, 1, 0, 0], {"generation": "gen2"})
    case("dotnet8.gen2_gc_frequent", "+4", False, K(ns=NS, pod="p2-g8-4"))
    counter("p2-g8-gone", "process.runtime.dotnet.gc.collections.count", 0, [5] * 9, {"generation": "gen2"}, last_ago=GONE)
    case("dotnet8.gen2_gc_frequent", "Pod 已刪除", False, K(ns=NS, pod="p2-g8-gone"))
    counter("p2-ex8-500", "process.runtime.dotnet.exceptions.count", 0, [0] * 11 + [100, 100, 100, 100, 100, 0, 0, 0, 0])
    case("dotnet8.exceptions_burst", "5 分鐘剛好 +500", True, K(ns=NS, pod="p2-ex8-500"))
    counter("p2-ex8-499", "process.runtime.dotnet.exceptions.count", 0, [0] * 11 + [100, 100, 100, 100, 99, 0, 0, 0, 0])
    case("dotnet8.exceptions_burst", "+499", False, K(ns=NS, pod="p2-ex8-499"))
    counter("p2-ex8-gone", "process.runtime.dotnet.exceptions.count", 0, [200] * 9, last_ago=GONE)
    case("dotnet8.exceptions_burst", "Pod 已刪除", False, K(ns=NS, pod="p2-ex8-gone"))

    # 35、36. Kafka：落後 >= 10,000（for 3m、0.8）；5 分鐘淨增加 >= 5,000
    def lag(cg, vals, last_ago=5):
        series(APP, {"service.name": "kafka-exporter", "k8s.namespace.name": NS}, vals, "kafka_consumergroup_lag_sum",
               last_ago, attrs={"consumergroup": cg, "topic": "p2-topic"})
    lag("p2-lag-10k", [100] * 10 + [10000] * 11)
    case("kafka.consumer_lag", "剛好 10,000、蓋滿 3 分鐘", True, K(cg="p2-lag-10k", topic="p2-topic"))
    lag("p2-lag-9999", [9999] * N10)
    case("kafka.consumer_lag", "9,999", False, K(cg="p2-lag-9999", topic="p2-topic"))
    lag("p2-lag-gone", [50000] * 10, last_ago=GONE)
    case("kafka.consumer_lag", "group 已不在", False, K(cg="p2-lag-gone", topic="p2-topic"))
    lag("p2-grow-5k", [10000] * 12 + [10000 + 5000 * i // 9 for i in range(1, 10)])   # for 5m 裡 10 點：10000 → 15000
    case("kafka.consumer_lag_growing", "5 分鐘淨增加剛好 5,000", True, K(cg="p2-grow-5k", topic="p2-topic"))
    lag("p2-grow-4999", [10000] * 11 + [10001 + 555 * i for i in range(9)] + [15000])
    case("kafka.consumer_lag_growing", "淨增加 4,999", False, K(cg="p2-grow-4999", topic="p2-topic"))
    lag("p2-grow-down", [20000 - 600 * i for i in range(N10)])
    case("kafka.consumer_lag_growing", "落後一直變少（追上來，量測值不是計數器）", False, K(cg="p2-grow-down", topic="p2-topic"))
    lag("p2-grow-gone", [1000 * i for i in range(10)], last_ago=GONE)
    case("kafka.consumer_lag_growing", "group 已不在", False, K(cg="p2-grow-gone", topic="p2-topic"))

    # 37～39. 機台（依 equipment_id）
    def eap(eq, field, vals, last_ago=5):
        series(APP, {"service.name": "p2-eap", "k8s.namespace.name": NS}, vals, field, last_ago, attrs={"equipment_id": eq})
    eap("EQ-DISC-A", "eap_secs_communicating", [1] * 10 + [0, 0])
    case("eap.secs_disconnected", "for 剛好：最後 2 點斷線", True, K(eq="EQ-DISC-A"))
    eap("EQ-DISC-B", "eap_secs_communicating", [1] * 10 + [0])
    case("eap.secs_disconnected", "只有最後 1 點斷線", False, K(eq="EQ-DISC-B"))
    eap("EQ-DISC-GONE", "eap_secs_communicating", [0] * 10, last_ago=GONE)
    case("eap.secs_disconnected", "機台資料停了 3 分鐘", False, K(eq="EQ-DISC-GONE"))
    eap("EQ-PLC-50", "plc_scan_cycle_time_ms", [8] * 10 + [50] * 11)
    case("eap.plc_scan_slow", "剛好 50ms、蓋滿 2 分鐘", True, K(eq="EQ-PLC-50"))
    eap("EQ-PLC-49", "plc_scan_cycle_time_ms", [49.9] * N10)
    case("eap.plc_scan_slow", "49.9ms", False, K(eq="EQ-PLC-49"))
    eap("EQ-PLC-GONE", "plc_scan_cycle_time_ms", [80] * 10, last_ago=GONE)
    case("eap.plc_scan_slow", "機台資料停了", False, K(eq="EQ-PLC-GONE"))
    eap("EQ-RTX-10", "eap_secs_retransmits_total", [0] * 12 + [1, 2, 3, 4, 5, 6, 7, 8, 10])
    case("eap.secs_retransmits", "5 分鐘剛好 +10", True, K(eq="EQ-RTX-10"))
    eap("EQ-RTX-9", "eap_secs_retransmits_total", [0] * 12 + [1, 2, 3, 4, 5, 6, 7, 8, 9])
    case("eap.secs_retransmits", "+9", False, K(eq="EQ-RTX-9"))
    eap("EQ-RTX-GONE", "eap_secs_retransmits_total", [10 * i for i in range(10)], last_ago=GONE)
    case("eap.secs_retransmits", "機台資料停了", False, K(eq="EQ-RTX-GONE"))


# ── ES ──

INFRA = (requests.ConnectionError, requests.Timeout)


def retry(fn, tries=8):
    """測試機很忙時 ES 偶爾連不上、逾時：重試（只限連線層的錯，ES 回的錯照樣丟出來）。"""
    import time
    for i in range(tries):
        try:
            return fn()
        except INFRA as e:
            if i == tries - 1:
                raise
            print(f"    （ES 連線失敗，{20 * (i + 1)} 秒後重試：{type(e).__name__}）")
            time.sleep(20 * (i + 1))


def req(method, path, body=None, ok=(200, 201), **kw):
    r = retry(lambda: s.request(method, ES_URL + path, json=body, timeout=600, **kw))
    if r.status_code not in ok:
        raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:800]}")
    return r.json() if r.content else {}


def load():
    # 已經有的資料流只清資料（delete_by_query 不用動 cluster state，機器忙的時候建、刪資料流一次要半分鐘以上）
    for name in (KC, KL, PR, NTP, APP):
        if req("GET", f"/_data_stream/{name}", ok=(200, 404)).get("data_streams"):
            req("POST", f"/{name}/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"match_all": {}}})
        else:
            req("PUT", f"/_data_stream/{name}?master_timeout=5m")
    for idx in ("aiops-anomalies", "aiops-tuning"):
        req("POST", f"/{idx}/_delete_by_query?refresh=true&conflicts=proceed&ignore_unavailable=true",
            {"query": {"match_all": {}}}, ok=(200, 404))
    req("GET", "/_cluster/health?wait_for_no_initializing_shards=true&timeout=5m")
    # 欄位一次建好（每個資料流一次 put-mapping）：讓 bulk 邊寫邊加欄位的話，每個新欄位都要排一次 master 的工作
    # 型別跟 Collector 寫進 ES 8.19 時自動建的一樣（看過已建好的 mapping：histogram 沒有 time_series_metric）；已經有的欄位不再送
    TYPES = {"gauge_double": {"type": "double", "meta": {"unit": ""}, "time_series_metric": "gauge"},
             HIST: {"type": HIST, "meta": {"unit": ""}}}
    props: dict[str, dict] = defaultdict(lambda: {"metrics": {}, "attributes": {}, "resource": {}})
    have = {}
    for index in (KC, KL, PR, NTP, APP):
        m = next(iter(req("GET", f"/{index}/_mapping").values()), {}).get("mappings", {}).get("properties", {})
        have[index] = {"metrics": set(m.get("metrics", {}).get("properties", {})),
                       "attributes": set(m.get("attributes", {}).get("properties", {})),
                       "resource": set(m.get("resource", {}).get("properties", {}).get("attributes", {}).get("properties", {}))}
    for index, doc, dyn in DOCS:
        p, h = props[index], have[index]
        for k in doc["metrics"]:
            if k not in h["metrics"]:
                p["metrics"][k] = TYPES[dyn[f"metrics.{k}"]]
        for k, v in doc["attributes"].items():
            if k not in h["attributes"]:
                p["attributes"][k] = {"type": "long", "time_series_dimension": True} if isinstance(v, int) else \
                    {"type": "keyword", "ignore_above": 1024, "time_series_dimension": True}
        for k in doc["resource"]["attributes"]:
            if k not in h["resource"]:
                p["resource"][k] = {"type": "keyword", "ignore_above": 1024, "time_series_dimension": True}
    for index, p in props.items():
        if not any(p.values()):
            continue
        req("PUT", f"/{index}/_mapping?master_timeout=5m", {"properties": {
            "metrics": {"type": "passthrough", "priority": 10, "properties": p["metrics"]},
            "attributes": {"type": "passthrough", "priority": 20, "time_series_dimension": True, "properties": p["attributes"]},
            "resource": {"properties": {"attributes": {"type": "passthrough", "priority": 40, "time_series_dimension": True,
                                                         "properties": p["resource"]}}}}})
    lines = []
    for index, doc, dyn in DOCS:
        meta = {"create": {"_index": index}}
        if dyn:
            meta["create"]["dynamic_templates"] = dyn
        lines += [json.dumps(meta), json.dumps(doc)]
    bad = []
    for i in range(0, len(lines), 1000):
        r = retry(lambda: s.post(f"{ES_URL}/_bulk?refresh=true", data="\n".join(lines[i:i + 1000]) + "\n",
                                 headers={"Content-Type": "application/x-ndjson"}, timeout=600)).json()
        # 409：重試時前一次其實寫進去了（TSDS 同一個對象、同一個時間只收一筆），不算失敗
        bad += [it["create"] for it in r.get("items", []) if it["create"].get("error") and it["create"].get("status") != 409]
    if bad:
        print(f"寫入失敗 {len(bad)} 筆，例：{json.dumps(bad[:2], ensure_ascii=False)[:1200]}")
        sys.exit(2)
    print(f"灌了 {len(DOCS)} 份文件（{len(CASES)} 個案例），NOW = {NOW.isoformat()}")


def run_rules(at=NOW, only=None, quiet=True):
    cmd = [sys.executable, "-X", "utf8", "-m", "detector", "rules", "--config", CONFIG, "--at", at.isoformat()]
    if only:
        cmd += ["--only", only]
    env = {**os.environ, "PYTHONUTF8": "1"}
    import re
    import time
    for i in range(6):   # ES 連線層的錯（測試機忙）才重跑；同一個 --at 重跑結果一樣（覆蓋同一個 _id）
        r = subprocess.run(cmd, cwd=ENGINE, capture_output=True, text=True, encoding="utf-8", env=env)
        if not re.search(r"ConnectionError|ReadTimeout|Read timed out|ConnectTimeout", r.stdout + r.stderr):
            break
        print(f"    （detector rules 連 ES 失敗，{30 * (i + 1)} 秒後重跑）")
        time.sleep(30 * (i + 1))
    if not quiet:
        print("    $ detector rules" + (f" --only {only}" if only else "") + f"（exit {r.returncode}）")
        for line in (r.stdout.strip().splitlines()[-6:] + r.stderr.strip().splitlines()[-3:]):
            print("      " + line)
    return r


def anomalies(slot=None) -> list[dict]:
    body = {"size": 10000, "query": {"bool": {"filter": [{"term": {"level": "rule"}}]
                                              + ([{"term": {"@timestamp": slot.isoformat()}}] if slot is not None else [])}}}
    return [h["_source"] | {"_id": h["_id"]} for h in
            req("POST", "/aiops-anomalies/_search?ignore_unavailable=true", body).get("hits", {}).get("hits", [])]


RESULTS: dict[str, list[bool]] = defaultdict(list)


def check(group: str, name: str, cond: bool, detail: str = ""):
    RESULTS[group].append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"：{detail}" if detail and not cond else ""))


# ── 1. 每條規則的觸發／不觸發 ──

def check_cases():
    print("== 1. 每條規則：觸發／不觸發（真的跑 detector rules --at NOW）")
    r = run_rules(quiet=False)
    got = anomalies(NOW.floor("1min"))
    fired = defaultdict(list)
    for a in got:
        fired[a["rule"]].append(a)
    with open(os.path.join(ENGINE, CONFIG), encoding="utf-8") as f:
        import yaml
        names = [x["name"] for x in yaml.safe_load(f)["rules"]]
    covered = defaultdict(lambda: [0, 0])
    for rule, desc, want, key in CASES:
        hits = [a for a in fired[rule] if all(a["keys"].get(k) == v for k, v in key.items())]
        covered[rule][0 if want else 1] += 1
        check("規則", f"{rule}｜{desc}｜預期{'觸發' if want else '不觸發'}", bool(hits) == want,
              f"實際{'觸發 ' + str(hits[0]['value']) if hits else '沒觸發'}")
    # context：服務名稱是 Pod label（資料裡的 service.name），根因分析靠它對到服務
    a = [x for x in fired["container.restarted"] if x["keys"].get("resource.attributes.k8s.pod.name") == "p2-crash-a"]
    check("規則", "context：container.restarted 的 keys 帶 service.name、node", bool(a) and
          a[0]["keys"].get("resource.attributes.service.name") == "p2-crash" and
          a[0]["keys"].get("resource.attributes.k8s.node.name") == "p2-node-1", json.dumps(a[0]["keys"] if a else {}))
    unknown = sorted({x["rule"] for x in got} - set(names))
    check("規則", "沒有 rules.yaml 以外的規則觸發", not unknown, str(unknown))
    missing_t = [n for n in names if covered[n][0] == 0]
    missing_n = [n for n in names if covered[n][1] == 0]
    check("規則", f"rules.yaml {len(names)} 條都有「觸發」案例", not missing_t, str(missing_t))
    check("規則", f"rules.yaml {len(names)} 條都有「不觸發」案例", not missing_n, str(missing_n))
    check("規則", "這一輪沒有規則執行失敗（exit 0）", r.returncode == 0, r.stdout[-400:] + r.stderr[-400:])
    return got


# ── 2. interval：同一個對象重跑不重複記 ──

def check_interval(first):
    print("== 2. interval：同一分鐘重跑不重複記；下一分鐘另記一筆")
    n1 = len(first)
    run_rules()
    again = anomalies(NOW.floor("1min"))
    check("interval", f"同一個 --at 再跑一次：筆數不變（{n1}）", len(again) == n1, f"變成 {len(again)}")
    check("interval", "_id 一樣（覆蓋，不是新增）", {a["_id"] for a in again} == {a["_id"] for a in first})
    run_rules(at=NOW + pd.Timedelta(seconds=10), only="container.restarted")
    same_min = anomalies(NOW.floor("1min"))
    check("interval", "同一分鐘晚 10 秒再跑：筆數不變", len(same_min) == n1, f"變成 {len(same_min)}")
    nxt = NOW + pd.Timedelta(minutes=1)
    run_rules(at=nxt, only="container.restarted")
    n_next = len(anomalies(nxt.floor("1min")))
    check("interval", "下一分鐘：container.restarted 另記一筆（每分鐘一筆，3 個對象）", n_next == 3, f"{n_next} 筆")


# ── 3. 動態調整介面：aiops-tuning 的 rule_suggestion ──

TUNED = "p2.tuned_lag"


def draft(**kw):
    return {"name": TUNED, "title": "（測試）p2 group 落後超過 5,000", "severity": "warning", "type": "value",
            "index": "metrics-*.otel-*", "field": "metrics.kafka_consumergroup_lag_sum", "op": ">=", "threshold": 5000,
            "by": ["attributes.consumergroup", "attributes.topic"],
            "filter": [{"term": {"attributes.consumergroup": "p2-lag-9999"}}], "_note": "e2e", **kw}


def tune_doc(_id, status, **kw):
    req("PUT", f"/aiops-tuning/_doc/{_id}?refresh=true",
        {"kind": "rule_suggestion", "status": status, "rule_draft": kw.pop("rule_draft", draft()), "source": "feedback",
         "at": pd.Timestamp.now(tz="UTC").isoformat(), **kw})


def tuned_fired(at=NOW) -> bool:
    return any(a["rule"] == TUNED for a in anomalies(at.floor("1min")))


def wipe_tuned():
    """這一節每次跑之前清掉兩條規則的紀錄（同一個 slot 會覆蓋，不清就看不出這一輪有沒有寫）。"""
    req("POST", "/aiops-anomalies/_delete_by_query?refresh=true",
        {"query": {"terms": {"rule": [TUNED, "kafka.consumer_lag"]}}}, ok=(200, 404))


def check_tuning():
    print("== 3. 動態調整介面：aiops-tuning 的規則草稿（rule_suggestion）")
    only = f"{TUNED},kafka.consumer_lag"
    req("DELETE", "/aiops-tuning", ok=(200, 404))
    wipe_tuned()
    r = run_rules(only=only)
    check("動態調整", "aiops-tuning 不存在：只跑 rules.yaml、exit 0", r.returncode == 0 and "觸發" in r.stdout,
          r.stdout[-300:] + r.stderr[-300:])
    from detector.adjust.tuning import TUNING_MAPPING  # noqa: E402  跟 adjust 寫的同一個 mapping
    req("PUT", "/aiops-tuning", TUNING_MAPPING)
    tune_doc("p2-rule", "proposed")
    wipe_tuned()
    r = run_rules(only=only)
    check("動態調整", "status proposed：不載入、不觸發", "載入" not in r.stdout and not tuned_fired(), r.stdout[-300:])
    a = subprocess.run([sys.executable, "-X", "utf8", "-m", "detector", "adjust", "approve", "--id", "p2-rule", "--by", "e2e"],
                       cwd=ENGINE, capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
    print("    $ detector adjust approve --id p2-rule： " + a.stdout.strip()[-120:])
    r = run_rules(only=only, quiet=False)
    check("動態調整", "adjust approve 後下一輪就載入並觸發（不用重啟、不用改 yaml）",
          f"載入 1 條核准的規則：{TUNED}" in r.stdout and tuned_fired(), r.stdout[-300:])
    wipe_tuned()
    for st in ("rejected", "inactive"):
        tune_doc("p2-rule", st)
        wipe_tuned()
        r = run_rules(only=only)
        check("動態調整", f"status {st}：不載入", "載入" not in r.stdout and not tuned_fired(), r.stdout[-300:])
    past = (NOW - pd.Timedelta(hours=1)).isoformat()      # 過期跟這一輪的時間（--at）比
    tune_doc("p2-rule", "active", expires_at=past)
    r = run_rules(only=only)
    check("動態調整", "active 但 expires_at 已過：不載入", "載入" not in r.stdout and not tuned_fired(), r.stdout[-300:])
    wipe_tuned()
    future = (NOW + pd.Timedelta(days=7)).isoformat()
    tune_doc("p2-rule", "active", expires_at=future)
    r = run_rules(only=only)
    check("動態調整", "active、expires_at 還沒到：載入並觸發", "載入 1 條" in r.stdout and tuned_fired(), r.stdout[-300:])
    wipe_tuned()
    tune_doc("p2-rule", "active", rule_draft={k: v for k, v in draft().items() if k != "index"})
    wipe_tuned()
    r = run_rules(only=only, quiet=False)
    yaml_ok = any(a["rule"] == "kafka.consumer_lag" for a in anomalies(NOW.floor("1min")))
    check("動態調整", "核准的草稿壞掉（缺 index）：只跳過那一條，rules.yaml 的照跑照寫",
          "p2.tuned_lag 失敗" in r.stdout and yaml_ok, (r.stdout[-300:] + r.stderr[-500:]))
    check("動態調整", "草稿壞掉時 exit 非 0（排程器看得到失敗）", r.returncode != 0, f"exit {r.returncode}")
    tune_doc("p2-rule", "active")
    req("POST", "/aiops-tuning/_close")
    wipe_tuned()
    r = run_rules(only=only, quiet=False)
    check("動態調整", "aiops-tuning 讀不到（index 關閉，ES 回 400）：只跑 rules.yaml、exit 0",
          r.returncode == 0 and "觸發" in r.stdout and not tuned_fired(), r.stdout[-300:] + r.stderr[-500:])
    req("POST", "/aiops-tuning/_open")


def main():
    build()
    load()
    first = check_cases()
    check_interval(first)
    check_tuning()
    print()
    total = sum(len(v) for v in RESULTS.values())
    for g, v in RESULTS.items():
        print(f"{g}：PASS {sum(v)}／{len(v)}")
    fails = total - sum(sum(v) for v in RESULTS.values())
    print("全部通過" if not fails else f"FAIL {fails} 項")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

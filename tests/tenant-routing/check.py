"""tenant-routing 的產生與檢查。情境只定義在這裡（CASES），run.sh 依它部署測試 app。

    python check.py cases                    印出每個情境的 namespace 和 tenant，給 run.sh 用
    python check.py agent <vmstack-values>   從 vmstack-values.yaml 加 site-ctsp.yaml 產生測試用的 VMAgent
    python check.py pruned <vmstack-values>  VMAgent 送進去的那幾段有沒有被 CRD 裁掉
    python check.py loaded <vmstack-values>  等 vmagent 實際載入的規則跟要測的一致
    python check.py labels                   chart 貼到 namespace 的 label 對不對
    python check.py routing [秒數]            每個 namespace 的資料在兩座各進了哪個 AccountID、帶什麼 site
    python check.py outage                   停掉站 1、中間重啟 vmagent、再恢復，停機期間的資料有沒有補回來

每一項都對 exit 0，任何一項不對 exit 1。
"""
import io
import json
import os
import subprocess
import sys
import time
from urllib.parse import quote

import yaml

CTX = "k3d-tenant-test"
AGENT_NS = "tr-central"
AGENT = "tenant-routing"
SITE_FILE = "platform/site-ctsp.yaml"      # 這套 vmagent 扮演中科的 cluster
SITE = "ctsp"
# 目的地 → 本機模擬的那一座在哪個 namespace。key 跟 vmstack-values.yaml 裡 remoteWrite 的 token key 一樣
SITES = {"ctsp": "tr-central", "erlin": "tr-site2"}
# 被測的設定裡要原樣送進 VMAgent 的幾段
KEYS = ("scrapeClasses", "inlineRelabelConfig", "statefulMode", "statefulStorage", "remoteWriteSettings",
        "remoteWrite")
WINDOW = "30s"      # 只看最近這段時間的樣本，不看上一輪留下來的

# 情境 → (values.yaml 寫的 tenant，None = 不寫；預期樣本上的 tenant)
CASES = {
    "tsid": ("tsid", "tsid"),
    "bsid": ("bsid", "bsid"),
    "icsd": ("icsd", "icsd"),
    "imc":  ("imc", "imc"),
    "none": (None, "platform"),       # 沒貼
    "typo": ("imcc", "platform"),     # 打錯。tests/validate.sh 會擋，這裡繞過它
}
SPOOF_NS = "tr-spoof"
# 預期的對照。跟 platform/vmstack-values.yaml 各寫一次是刻意的：
# 測試從被測的檔案讀預期值的話，檔案改錯了測試也會跟著錯。
EXPECT = {"platform": "1000", "tsid": "1001", "bsid": "1002", "icsd": "1003", "imc": "1004"}
ACCOUNTS = ["0"] + sorted(EXPECT.values())

failed = 0


def namespace(case):
    return "tr-%s" % case


def write_url(site):
    return "http://vmauth.%s.svc:8427/insert/multitenant/prometheus/api/v1/write" % SITES[site]


def kubectl(*args, check=True):
    env = dict(os.environ, MSYS_NO_PATHCONV="1")   # Git Bash 會把開頭是 / 的參數改成 Windows 路徑
    out = subprocess.run(["kubectl", "--context", CTX, *args],
                         capture_output=True, text=True, encoding="utf-8", env=env)
    if check and out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout


def report(ok, title, detail=()):
    global failed
    print("  %s  %s" % ("OK  " if ok else "FAIL", title), flush=True)
    for d in detail:
        print("        %s" % d, flush=True)
    failed += not ok


def load(path):
    with io.open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def agent(values):
    """測試用的 VMAgent：那幾段照 vmstack-values.yaml，疊上廠區的小檔，只換 url。"""
    spec = load(values)["vmagent"]["spec"]
    out = {k: spec[k] for k in KEYS if k in spec}
    # 每條 remoteWrite 依它的 token key 對到本機的那一座
    out["remoteWrite"] = [dict({k: v for k, v in rw.items() if k != "tlsConfig"},
                               url=write_url(rw["bearerTokenSecret"]["key"]))
                          for rw in out["remoteWrite"]]
    out["externalLabels"] = dict(spec.get("externalLabels") or {},
                                 **load(SITE_FILE)["vmagent"]["spec"]["externalLabels"])
    out.update(scrapeInterval="10s", selectAllByDefault=True)
    return {"apiVersion": "operator.victoriametrics.com/v1beta1", "kind": "VMAgent",
            "metadata": {"name": AGENT, "namespace": AGENT_NS}, "spec": out}


def pruned(values):
    """送進去的那幾段跟 API server 存下來的要一樣。kubectl explain 不能當依據，要比實際存的。"""
    want = agent(values)["spec"]
    got = json.loads(kubectl("-n", AGENT_NS, "get", "vmagent", AGENT, "-o", "json"))["spec"]
    for k in KEYS + ("externalLabels",):
        if k not in want:
            continue
        report(got.get(k) == want[k], "VMAgent.spec.%s 沒有被裁掉" % k,
               [] if got.get(k) == want[k] else ["送進去 %s" % json.dumps(want[k], ensure_ascii=False),
                                                "存下來 %s" % json.dumps(got.get(k), ensure_ascii=False)])


def agent_pod():
    out = kubectl("-n", AGENT_NS, "get", "pod", "-l", "app.kubernetes.io/name=vmagent",
                  "--field-selector=status.phase=Running", "-o", "jsonpath={.items[*].metadata.name}", check=False)
    return (out.split() or [None])[0]


def agent_get(path):
    """打 vmagent 自己的 HTTP 介面。pod 還沒起來或正在換的時候回空字串。"""
    pod = agent_pod()
    if not pod:
        return ""
    return kubectl("get", "--raw", "/api/v1/namespaces/%s/pods/%s:8429/proxy/%s" % (AGENT_NS, pod, path), check=False)


def loaded(values, wait=240):
    """operator 更新設定後 vmagent 不會馬上重新載入。等它實際載入的規則跟要測的一樣再往下。"""
    want = load(values)["vmagent"]["spec"]["inlineRelabelConfig"]
    deadline, got = time.time() + wait, None
    while time.time() < deadline:
        try:
            got = yaml.safe_load(agent_get("remotewrite-relabel-config")) or []
        except yaml.YAMLError:
            got = None
        if got == want:
            break
        time.sleep(5)
    report(got == want, "vmagent 實際載入的規則跟 %s 一樣（%d 條）" % (values, len(want)),
           [] if got == want else ["實際載入 %s" % json.dumps(got, ensure_ascii=False)])


def labels():
    for case, (tenant, _) in CASES.items():
        ns = json.loads(kubectl("get", "ns", namespace(case), "-o", "json"))
        got = (ns["metadata"].get("labels") or {}).get("tenant")
        report(got == tenant, "%-9s tenant=%s" % (namespace(case), got), [] if got == tenant else ["預期 %s" % tenant])


def query(site, account, expr, at=None):
    url = "http://127.0.0.1:8481/select/%s/prometheus/api/v1/query?query=%s" % (account, quote(expr))
    if at:
        url += "&time=%d" % at
    out = kubectl("-n", SITES[site], "exec", "vmselect-central-0", "--", "wget", "-qO-", url)
    return json.loads(out)["data"]["result"]


def where(site, ns, metric):
    """這個 namespace 最近的樣本在這一座的哪些 AccountID，各帶什麼 tenant 和 site。"""
    found = {}
    for account in ACCOUNTS:
        rows = query(site, account,
                     'count by (tenant, site) (last_over_time(%s{namespace="%s"}[%s]))' % (metric, ns, WINDOW))
        if rows:
            found[account] = sorted("%s/%s" % (r["metric"].get("tenant", ""), r["metric"].get("site", ""))
                                    for r in rows)
    return found


def routing(wait):
    targets = [(namespace(c), "eqp_up", tenant, EXPECT[tenant]) for c, (_, tenant) in CASES.items()]
    targets.append((SPOOF_NS, "spoof_value", "platform", EXPECT["platform"]))
    deadline = time.time() + wait
    while True:   # 等每個 namespace 在兩座都有資料；進錯地方也算有資料，不會一直等
        seen = {(s, ns): where(s, ns, metric) for s in SITES for ns, metric, _, _ in targets}
        if all(seen.values()) or time.time() > deadline:
            break
        time.sleep(10)
    for ns, _, tenant, account in targets:
        want = {account: ["%s/%s" % (tenant, SITE)]}
        got = {s: seen[(s, ns)] for s in SITES}
        ok = all(g == want for g in got.values())
        shown = "  ".join("%s站 %s" % (s, " ".join("%s(%s)" % (a, ",".join(t)) for a, t in sorted(g.items()))
                                      or "沒有資料") for s, g in got.items())
        note = "  ← 偽造：自帶 tenant=tsid、vm_account_id=1001、site=fake" if ns == SPOOF_NS else ""
        report(ok, "%-9s → %s%s" % (ns, shown, note),
               [] if ok else ["預期兩座都只在 %s，tenant=%s、site=%s" % (account, tenant, SITE)])
    for s in SITES:
        stray = query(s, "0", 'count({__name__!=""})')
        report(not stray, "%s站：AccountID 0 是空的（沒有樣本漏掉 vm_account_id）" % s,
               [] if not stray else ["有 %s 筆" % stray[0]["value"][1]])
        nosite = sum(float(r[0]["value"][1]) for r in
                     (query(s, a, 'count(last_over_time({__name__!="",site=""}[%s]))' % WINDOW)
                      for a in ACCOUNTS[1:]) if r)
        report(not nosite, "%s站：沒有不帶 site 的樣本" % s, [] if not nosite else ["有 %d 筆" % nosite])


def recent(site):
    rows = query(site, EXPECT["icsd"], 'sum(count_over_time(eqp_up{namespace="tr-icsd"}[%s]))' % WINDOW)
    return float(rows[0]["value"][1]) if rows else 0.0


def pending():
    """vmagent 還沒送出去、排在佇列裡的資料量（bytes），依目的地。"""
    total = {}
    for line in agent_get("metrics").splitlines():
        if line.startswith("vmagent_remotewrite_pending_data_bytes{"):
            key = "站 1" if 'url="1:' in line else "站 2"
            total[key] = total.get(key, 0) + float(line.rsplit(" ", 1)[1])
    return total


def entry(replicas):
    """站 1 的入口（vmauth）。0 = 模擬中科站停機或跨廠斷線。"""
    kubectl("-n", SITES["ctsp"], "scale", "deploy/vmauth", "--replicas=%d" % replicas)


def outage():
    before = {s: recent(s) for s in SITES}
    report(all(before.values()), "開始前兩座都有資料進來（最近 %s：%s）"
           % (WINDOW, "、".join("%s站 %d 筆" % (s, n) for s, n in before.items())))
    if failed:
        return
    try:
        t0 = time.time()
        entry(0)
        print("  --    站 1 的入口關掉了，等 60 秒", flush=True)
        time.sleep(60)
        during = {s: recent(s) for s in SITES}
        report(during["ctsp"] == 0 and during["erlin"] > 0,
               "停機中：站 1 沒有新資料（%d 筆），站 2 照常（%d 筆）" % (during["ctsp"], during["erlin"]))
        p1 = pending()
        report(p1.get("站 1", 0) > 0, "停機中：送不出去的資料排在佇列裡（%s）"
               % "、".join("%s %d KB" % (k, v / 1024) for k, v in sorted(p1.items())))

        kubectl("-n", AGENT_NS, "delete", "pod", "-l", "app.kubernetes.io/name=vmagent", "--wait=true")
        deadline = time.time() + 180
        while time.time() < deadline and not agent_get("metrics"):
            time.sleep(5)
        time.sleep(30)
        p2 = pending()
        report(p2.get("站 1", 0) >= p1.get("站 1", 0),
               "vmagent 重啟後：站 1 的佇列還在（重啟前 %d KB，重啟後 %d KB）"
               % (p1.get("站 1", 0) / 1024, p2.get("站 1", 0) / 1024),
               [] if p2.get("站 1", 0) >= p1.get("站 1", 0) else ["重啟前排隊的資料不見了"])
        t1 = time.time()
    finally:
        entry(1)
    kubectl("-n", SITES["ctsp"], "rollout", "status", "deploy/vmauth", "--timeout=120s")
    print("  --    站 1 的入口開回來了，等 90 秒讓它補送", flush=True)
    time.sleep(90)
    span = int(t1 - t0)
    expr = 'sum(count_over_time(eqp_up{namespace="tr-icsd"}[%ds]))' % span
    got = {s: query(s, EXPECT["icsd"], expr, at=int(t1)) for s in SITES}
    n = {s: float(r[0]["value"][1]) if r else 0.0 for s, r in got.items()}
    ok = n["erlin"] > 0 and n["ctsp"] >= 0.95 * n["erlin"]
    report(ok, "補送：停機的 %d 秒內，站 1 有 %d 筆、站 2 有 %d 筆" % (span, n["ctsp"], n["erlin"]),
           [] if ok else ["站 1 少於站 2 的 95%，停機期間的資料沒有補回來"])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "cases":
        for case, (tenant, _) in CASES.items():
            print(namespace(case), tenant or "-")
        return 0
    if cmd == "agent":
        yaml.safe_dump(agent(sys.argv[2]), sys.stdout, allow_unicode=True, sort_keys=False)
        return 0
    if cmd == "pruned":
        pruned(sys.argv[2])
    elif cmd == "loaded":
        loaded(sys.argv[2])
    elif cmd == "labels":
        labels()
    elif cmd == "routing":
        routing(int(sys.argv[2]) if len(sys.argv) > 2 else 180)
    elif cmd == "outage":
        outage()
    else:
        print(__doc__)
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

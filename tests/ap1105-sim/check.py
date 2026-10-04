"""AP1105 模擬的檢查。全部透過 kubectl get --raw 走 API server proxy，不需要 port-forward。

    python check.py state                    VMAgent 現況：remoteWrite 幾條、有沒有 scrapeClasses
    python check.py targets none|set [--wait N]
                                             eqp 系列 target 都是 up；none = 還沒有 dept/group/system，
                                             set = group 跟 namespace 的 label 一樣
    python check.py relabel routed|single [--wait N]
                                             vmagent 實際載入的 remoteWrite 規則：routed = 跟 vmstack-values.yaml
                                             一模一樣的三條；single = 回到一條、沒有規則
    python check.py remotewrite [--wait N]   三條 remoteWrite 都送成功（2xx），沒有 401 / 403
    python check.py diff <before> <dry-run>  dry-run 只改到 VMAgent 的 remoteWrite / scrapeClasses
    python check.py routing [--wait N]       每個 namespace 進了哪些 tenant，跟 EXPECT 一樣
    python check.py fanout                   it.imc 的同一筆原始樣本在 IT、IMC 兩邊一模一樣
    python check.py platform                 沒貼 group 的 namespace 只在 tenant 0
    python check.py synced [--expect R,C]    sync-job 建的 VMRule / dashboard ConfigMap 還剩多少（關 syncJob 會不會把它們刪掉）
    python check.py leak                     舊 CRD 裁掉過濾之後，IT / IMC 收到了不該收的資料
    python check.py crd                      VMAgent CRD 是「不宣告欄位、全部照收」還是有完整 schema
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import yaml

CTX = "k3d-ap1105"
NS = "vm-monitoring"
VMAGENT = "vmstack-victoria-metrics-k8s-stack"
VMAGENT_SVC = "vmagent-%s:8429" % VMAGENT
VMSELECT_SVC = "vmselect-%s:8481" % VMAGENT
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")

# 跟 central-vmauth.yaml 的 url_prefix 一致
TENANTS = {"it": 2, "imc": 1, "platform": 0}

# namespace → (namespace 上的 group, 預期進哪些 tenant)
EXPECT = {
    "eqp-collector-dev": ("imc", {"imc"}),
    "eqp-sim-it": ("it", {"it"}),
    "eqp-sim-shared": ("it.imc", {"it", "imc"}),
    "eqp-sim-none": (None, {"platform"}),
    # 打錯字：現在的 regex 以「點切開的段」比對，it 這段對到，所以只進 IT、不會進 platform
    "eqp-sim-typo": ("it.imcc", {"it"}),
}


def kubectl(*args):
    out = subprocess.run(["kubectl", "--context", CTX] + list(args),
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode:
        raise RuntimeError(out.stderr.strip())
    return out.stdout


def svc(name, path):
    return kubectl("get", "--raw", "/api/v1/namespaces/%s/app/%s/proxy%s" % (NS, name, path))


def query(tenant, q):
    path = "/select/%d/prometheus/api/v1/query?%s" % (TENANTS[tenant], urllib.parse.urlencode({"query": q}))
    return json.loads(svc(VMSELECT_SVC, path))["data"]["result"]


def poll(fn, wait):
    """fn() 回傳 (ok, 要印的行)。wait 秒內一直重試，最後一次的結果印出來。"""
    deadline = time.time() + wait
    while True:
        try:
            ok, lines = fn()
        except RuntimeError as e:
            ok, lines = False, ["（查詢失敗）%s" % e]
        if ok or time.time() >= deadline:
            for line in lines:
                print("  " + line)
            print("  %s" % ("PASS" if ok else "FAIL"))
            return ok
        time.sleep(10)


def state():
    s = json.loads(kubectl("-n", NS, "get", "vmagent", VMAGENT, "-o", "json"))["spec"]
    print("  remoteWrite：%d 條" % len(s.get("remoteWrite", [])))
    for rw in s.get("remoteWrite", []):
        key = (rw.get("bearerTokenSecret") or {}).get("key")
        print("    - %s%s" % (rw["url"], "  token=%s" % key if key else ""))
    print("  scrapeClasses：%s" % ([c["name"] for c in s.get("scrapeClasses", [])] or "沒有"))
    print("  externalLabels：%s" % s.get("externalLabels"))
    return True


def targets(mode):
    def fn():
        rows = [t for t in json.loads(svc(VMAGENT_SVC, "/api/v1/targets"))["data"]["activeTargets"]
                if t["labels"].get("namespace") in EXPECT]
        lines, ok = [], len(rows) == len(EXPECT)
        for t in sorted(rows, key=lambda t: t["labels"]["namespace"]):
            l = t["labels"]
            want = None if mode == "none" else EXPECT[l["namespace"]][0]
            good = t["health"] == "up" and l.get("group") == want
            if mode == "none":
                good = good and not any(l.get(k) for k in ("dept", "system"))
            ok = ok and good
            lines.append("%-4s health=%-4s %-18s dept=%-6s group=%-8s system=%s" % (
                "OK" if good else "FAIL", t["health"], l["namespace"], l.get("dept"), l.get("group"), l.get("system")))
        if len(rows) != len(EXPECT):
            lines.append("預期 %d 個 target，實際 %d 個" % (len(EXPECT), len(rows)))
        return ok, lines
    return fn


def relabel(mode):
    want = yaml.safe_load(io.open(os.path.join(ROOT, "platform", "vmstack-values.yaml"), encoding="utf-8"))
    want = [rw["inlineUrlRelabelConfig"] for rw in want["vmagent"]["spec"]["remoteWrite"]]

    def fn():
        got = yaml.safe_load(svc(VMAGENT_SVC, "/remotewrite-url-relabel-config")) or []
        rules = [g.get("relabel_config") or [] for g in got]
        lines = ["%s：%s" % (g["url"], ", ".join("%s %s" % (r.get("action", "replace"), r.get("regex", r.get("replacement")))
                                               for r in g.get("relabel_config") or []) or "沒有規則") for g in got]
        if mode == "single":
            # 沒有任何 url relabel 時，vmagent 可能整段不印
            return len(got) <= 1 and not any(rules), lines or ["沒有任何 url relabel 規則"]
        return rules == want, lines + ([] if rules == want else ["跟 platform/victoriametrics/vmstack-values.yaml 的規則不一樣"])
    return fn


def remotewrite():
    pat = re.compile(r'^vmagent_remotewrite_requests_total\{(.*)\} (\S+)$')

    def fn():
        by_url = {}
        for line in svc(VMAGENT_SVC, "/metrics").splitlines():
            m = pat.match(line)
            if not m:
                continue
            labels = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            code = labels.get("status_code", "")
            by_url.setdefault(labels.get("url"), {}).setdefault(code, 0)
            by_url[labels.get("url")][code] += float(m.group(2))
        lines = ["%s  %s" % (u, "  ".join("%s=%d" % kv for kv in sorted(c.items()))) for u, c in sorted(by_url.items())]
        ok = len(by_url) == 3 and all(any(k.startswith("2") and v > 0 for k, v in c.items())
                                     and not any(k in ("401", "403") and v > 0 for k, v in c.items())
                                     for c in by_url.values())
        return ok, lines
    return fn


def load_manifest(text):
    docs = {}
    for d in yaml.safe_load_all(text):
        if d:
            docs[(d["kind"], (d.get("metadata") or {}).get("namespace"), d["metadata"]["name"])] = d
    return docs


def diff(before_path, dry_path):
    before = load_manifest(io.open(before_path, encoding="utf-8").read())
    dry = io.open(dry_path, encoding="utf-8").read()
    if "\nMANIFEST:\n" not in dry:
        print("  dry-run 輸出裡找不到 MANIFEST:")
        print("  FAIL")
        return False
    after = load_manifest(dry.split("\nMANIFEST:\n", 1)[1].split("\nNOTES:\n", 1)[0])
    added, removed = sorted(set(after) - set(before)), sorted(set(before) - set(after))
    changed = sorted(k for k in before if k in after and before[k] != after[k])
    print("  資源：%d → %d，新增 %s，刪除 %s" % (len(before), len(after), added or "無", removed or "無"))
    ok = not added and not removed
    for k in changed:
        a, b = before[k].get("spec") or before[k].get("data"), after[k].get("spec") or after[k].get("data")
        fields = sorted(f for f in set(a or {}) | set(b or {}) if (a or {}).get(f) != (b or {}).get(f))
        expected = k[0] == "VMAgent" and set(fields) <= {"remoteWrite", "scrapeClasses"}
        ok = ok and expected
        print("  %-4s 有變：%s %s  欄位 %s" % ("OK" if expected else "FAIL", k[0], k[2], fields or "（metadata）"))
    print("  %s" % ("PASS" if ok else "FAIL"))
    return ok


def routing():
    def fn():
        actual = {ns: set() for ns in EXPECT}
        for tenant in TENANTS:
            for r in query(tenant, "count by (namespace) (count_over_time(eqp_up[1m]))"):
                ns = r["metric"].get("namespace")
                if ns in actual:
                    actual[ns].add(tenant)
        lines, ok = [], True
        for ns, (group, want) in EXPECT.items():
            good = actual[ns] == want
            ok = ok and good
            lines.append("%-4s %-18s group=%-8s %s" % ("OK" if good else "FAIL", ns, group,
                         "  ".join("%s=%s" % (t, "✓" if t in actual[ns] else "-") for t in TENANTS)))
        return ok, lines
    return fn


def fanout():
    start = int(time.time()) - 180
    match = 'eqp_up{namespace="eqp-sim-shared",machine="EQP001"}'
    got = {}
    for tenant in ("it", "imc"):
        path = "/select/%d/prometheus/api/v1/export?%s" % (
            TENANTS[tenant], urllib.parse.urlencode({"match[]": match, "start": start}))
        samples = {}
        for line in svc(VMSELECT_SVC, path).splitlines():
            if line.strip():
                d = json.loads(line)
                samples.update(zip(d["timestamps"], d["values"]))
        got[tenant] = samples
    if not got["it"] or not got["imc"]:
        print("  其中一邊沒有資料：it=%d imc=%d" % (len(got["it"]), len(got["imc"])))
        print("  FAIL")
        return False
    # 兩條 remoteWrite 各自送，最新的幾筆可能還在路上，只比兩邊都到了的時間範圍
    until = min(max(got["it"]), max(got["imc"]))
    a = {t: v for t, v in got["it"].items() if t <= until}
    b = {t: v for t, v in got["imc"].items() if t <= until}
    ok = a == b and len(a) >= 2
    print("  IT %d 筆、IMC %d 筆，timestamp（毫秒）和值%s" % (len(a), len(b), "完全一樣" if a == b else "不一樣"))
    print("  %s" % ("PASS" if ok else "FAIL"))
    return ok


def platform():
    ok = True
    for tenant in TENANTS:
        rows = query(tenant, 'count by (namespace) (count_over_time(up{namespace!~"eqp-.*"}[1m]))')
        found = sorted(r["metric"].get("namespace", "（無）") for r in rows)
        good = bool(found) if tenant == "platform" else not found
        ok = ok and good
        print("  %-4s %-8s 沒貼 group 的 namespace：%s" % ("OK" if good else "FAIL", tenant, ", ".join(found) or "無"))
    # 分流看的是「被抓的 target 在哪個 namespace」，不是 metrics 講的是誰。
    # kube-state-metrics 在 vm-monitoring（沒貼 group），所以講 eqp pod 狀態的 metrics 也在 platform
    for tenant in TENANTS:
        n = len(query(tenant, 'count_over_time(kube_pod_info{namespace="eqp-collector-dev"}[1m])'))
        print("  （參考）%-8s 有沒有 eqp-collector-dev 的 kube_pod_info：%s" % (tenant, "有" if n else "沒有"))
    print("  %s" % ("PASS" if ok else "FAIL"))
    return ok


def synced(expect):
    """sync-job 建出來的東西（VMRule、給 Grafana 的 dashboard ConfigMap）還剩多少、owner 是誰。

    用 label 算，不是用 owner 算：orphan 之後 owner 會被拿掉，但東西還在。
    """
    sel = "app.kubernetes.io/managed-by=sync-job"
    rules = json.loads(kubectl("get", "vmrules", "-A", "-l", sel, "-o", "json"))["items"]
    cms = json.loads(kubectl("get", "configmaps", "-A", "-l", sel, "-o", "json"))["items"]
    owners = sorted({"%s/%s" % (o["kind"], o["name"]) for i in rules + cms for o in i["metadata"].get("ownerReferences", [])})
    print("  sync-job 建的：VMRule %d 個、dashboard ConfigMap %d 個，owner：%s" % (len(rules), len(cms), ", ".join(owners) or "無"))
    if expect is None:
        print("  COUNT %d,%d" % (len(rules), len(cms)))
        return True
    ok = "%d,%d" % (len(rules), len(cms)) == expect
    print("  %s（預期 VMRule,ConfigMap 還是 %s）" % ("PASS" if ok else "FAIL", expect))
    return ok


def leak():
    """舊 CRD 會把 inlineUrlRelabelConfig 裁掉：三條 remoteWrite 都沒有過濾，每一筆都進三個 tenant。
    確認 IT / IMC 收到了不該收的東西（沒 group 的 eqp-sim-none、kube-system）。"""
    ok = True
    for tenant in ("it", "imc"):
        rows = query(tenant, 'count by (namespace) (count_over_time(up{namespace=~"eqp-sim-none|kube-system"}[2m]))')
        found = sorted(r["metric"]["namespace"] for r in rows)
        ok = ok and bool(found)
        print("  %-4s %s 收到了不該收的：%s" % ("OK" if found else "FAIL", tenant, ", ".join(found) or "無"))
    print("  %s（預期：過濾被裁掉，資料外洩到每個 tenant）" % ("PASS" if ok else "FAIL"))
    return ok


def crd():
    """AP1105 上 kubectl explain 說欄位不存在，要分清楚是哪一種 CRD。"""
    c = json.loads(kubectl("get", "crd", "vmagents.operator.victoriametrics.com", "-o", "json"))
    for v in c["spec"]["versions"]:
        s = v["schema"]["openAPIV3Schema"]["properties"]["spec"]
        p = s.get("properties", {})
        rw = ((p.get("remoteWrite") or {}).get("items") or {}).get("properties", {})
        print("  %s：preserve-unknown-fields=%s，宣告了 %d 個欄位，scrapeClasses %s，remoteWrite.inlineUrlRelabelConfig %s" % (
            v["name"], bool(s.get("x-kubernetes-preserve-unknown-fields")), len(p),
            "有" if "scrapeClasses" in p else "沒有", "有" if "inlineUrlRelabelConfig" in rw else "沒有"))
    return True


if __name__ == "__main__":
    args = sys.argv[1:]
    wait = 0
    if "--wait" in args:
        i = args.index("--wait")
        wait = int(args[i + 1])
        del args[i:i + 2]
    cmd = args[0]
    if cmd == "state":
        ok = state()
    elif cmd == "targets":
        ok = poll(targets(args[1]), wait)
    elif cmd == "relabel":
        ok = poll(relabel(args[1]), wait)
    elif cmd == "remotewrite":
        ok = poll(remotewrite(), wait)
    elif cmd == "diff":
        ok = diff(args[1], args[2])
    elif cmd == "routing":
        ok = poll(routing(), wait)
    elif cmd == "fanout":
        ok = fanout()
    elif cmd == "platform":
        ok = platform()
    elif cmd == "synced":
        expect = None
        if "--expect" in args:
            expect = args[args.index("--expect") + 1]
        ok = synced(expect)
    elif cmd == "leak":
        ok = leak()
    elif cmd == "crd":
        ok = crd()
    else:
        sys.exit("不認得的指令：%s" % cmd)
    sys.exit(0 if ok else 1)

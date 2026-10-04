"""tenant-routing 的產生與檢查。情境只定義在這裡（CASES），run.sh 依它部署測試 app。

    python check.py cases                    印出每個情境的 namespace 和 tenant，給 run.sh 用
    python check.py agent <vmstack-values>   從 vmstack-values.yaml 產生測試用的 VMAgent
    python check.py pruned <vmstack-values>  VMAgent 送進去的分流三段有沒有被 CRD 裁掉
    python check.py loaded <vmstack-values>  等 vmagent 實際載入的規則跟要測的一致
    python check.py labels                   chart 貼到 namespace 的 label 對不對
    python check.py routing [秒數]            每個 namespace 的資料進了哪個 AccountID

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
CENTRAL = "tr-central"
AGENT = "tenant-routing"
WRITE_URL = "http://vmauth.%s.svc:8427/insert/multitenant/prometheus/api/v1/write" % CENTRAL
ROUTING_KEYS = ("scrapeClasses", "inlineRelabelConfig", "remoteWrite")
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


def kubectl(*args, check=True):
    env = dict(os.environ, MSYS_NO_PATHCONV="1")   # Git Bash 會把開頭是 / 的參數改成 Windows 路徑
    out = subprocess.run(["kubectl", "--context", CTX, *args],
                         capture_output=True, text=True, encoding="utf-8", env=env)
    if check and out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout


def report(ok, title, detail=()):
    global failed
    print("  %s  %s" % ("OK  " if ok else "FAIL", title))
    for d in detail:
        print("        %s" % d)
    failed += not ok


def load(path):
    with io.open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def agent(values):
    """測試用的 VMAgent：分流三段照 vmstack-values.yaml，只換 url。"""
    spec = load(values)["vmagent"]["spec"]
    out = {k: spec[k] for k in ROUTING_KEYS}
    out["remoteWrite"] = [dict({k: v for k, v in rw.items() if k != "tlsConfig"}, url=WRITE_URL)
                          for rw in out["remoteWrite"]]
    out.update(scrapeInterval="10s", selectAllByDefault=True)
    return {"apiVersion": "operator.victoriametrics.com/v1beta1", "kind": "VMAgent",
            "metadata": {"name": AGENT, "namespace": CENTRAL}, "spec": out}


def pruned(values):
    """送進去的分流三段跟 API server 存下來的要一樣。kubectl explain 不能當依據，要比實際存的。"""
    want = agent(values)["spec"]
    got = json.loads(kubectl("-n", CENTRAL, "get", "vmagent", AGENT, "-o", "json"))["spec"]
    for k in ROUTING_KEYS:
        report(got.get(k) == want[k], "VMAgent.spec.%s 沒有被裁掉" % k,
               [] if got.get(k) == want[k] else ["送進去 %s" % json.dumps(want[k], ensure_ascii=False),
                                                "存下來 %s" % json.dumps(got.get(k), ensure_ascii=False)])


def loaded(values, wait=180):
    """operator 更新設定後 vmagent 不會馬上重新載入。等它實際載入的規則跟要測的一樣再往下。"""
    want = load(values)["vmagent"]["spec"]["inlineRelabelConfig"]
    pod = kubectl("-n", CENTRAL, "get", "pod", "-l", "app.kubernetes.io/name=vmagent",
                  "--field-selector=status.phase=Running", "-o", "jsonpath={.items[0].metadata.name}")
    deadline, got = time.time() + wait, None
    while time.time() < deadline:
        raw = kubectl("get", "--raw", "/api/v1/namespaces/%s/pods/%s:8429/proxy/remotewrite-relabel-config"
                      % (CENTRAL, pod), check=False)
        try:
            got = yaml.safe_load(raw) or []
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


def query(account, expr):
    url = "http://127.0.0.1:8481/select/%s/prometheus/api/v1/query?query=%s" % (account, quote(expr))
    out = kubectl("-n", CENTRAL, "exec", "vmselect-central-0", "--", "wget", "-qO-", url)
    return json.loads(out)["data"]["result"]


def where(ns, metric):
    """這個 namespace 最近的樣本在哪些 AccountID，各帶什麼 tenant。"""
    found = {}
    for account in ACCOUNTS:
        rows = query(account, 'count by (tenant) (last_over_time(%s{namespace="%s"}[%s]))' % (metric, ns, WINDOW))
        if rows:
            found[account] = sorted(r["metric"].get("tenant", "") for r in rows)
    return found


def routing(wait):
    targets = [(namespace(c), "eqp_up", tenant, EXPECT[tenant]) for c, (_, tenant) in CASES.items()]
    targets.append((SPOOF_NS, "spoof_value", "platform", EXPECT["platform"]))
    deadline = time.time() + wait
    while True:   # 等每個 namespace 都有資料進來；進錯地方也算有資料，不會一直等
        seen = {ns: where(ns, metric) for ns, metric, _, _ in targets}
        if all(seen.values()) or time.time() > deadline:
            break
        time.sleep(10)
    for ns, _, tenant, account in targets:
        got, want = seen[ns], {account: [tenant]}
        shown = " ".join("%s(tenant=%s)" % (a, ",".join(t)) for a, t in sorted(got.items())) or "沒有資料"
        note = "  ← 偽造：自帶 tenant=tsid、vm_account_id=1001" if ns == SPOOF_NS else ""
        report(got == want, "%-9s → %s%s" % (ns, shown, note),
               [] if got == want else ["預期只在 %s，tenant=%s" % (account, tenant)])
    stray = query("0", 'count({__name__!=""})')
    report(not stray, "AccountID 0 是空的（沒有樣本漏掉 vm_account_id）",
           [] if not stray else ["有 %s 筆" % stray[0]["value"][1]])


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
    else:
        print(__doc__)
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

"""E2E 的檢查。情境只定義在這裡（CASES），gen.py 依它產生測試用的 app。

    python check.py config <產生的 vmagent.yaml>   等 vmagent 實際載入的分流規則跟要測的一致
    python check.py labels                        ArgoCD 貼到 namespace 的 label 對不對
    python check.py routing [--wait 秒數]          每個 namespace 進了哪些 tenant、共用的是不是同一次抓取

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

TENANTS = ["it", "imc", "platform"]
OBS = ["dept", "group", "system"]

# 情境 → (values.yaml 的 observability, 預期進哪些 tenant)
CASES = {
    "it":     ({"dept": "D3120", "group": "it", "system": "sim"},  {"it"}),
    "imc":    ({"dept": "D1110", "group": "imc", "system": "sim"}, {"imc"}),
    "shared": ({"dept": "D3120", "group": "it.imc"},               {"it", "imc"}),   # 故意不寫 system
    "none":   (None,                                               {"platform"}),    # 沒有 observability
    "bad":    ({"dept": "D9999", "group": "nope"},                 {"platform"}),    # CI 會擋，這裡繞過它
}
SHARED = "shared"


def namespace(case):
    return "zk-sim-%s-prod" % case


def kubectl(*args):
    env = dict(os.environ, MSYS_NO_PATHCONV="1")   # Git Bash 會把開頭是 / 的參數改成 Windows 路徑
    out = subprocess.run(["kubectl", *args], capture_output=True, text=True, encoding="utf-8", env=env)
    if out.returncode != 0:
        raise RuntimeError("kubectl %s 失敗：%s" % (" ".join(args), out.stderr.strip()))
    return out.stdout


# ── config：改了設定不會馬上生效 ──────────────────────────────────
# operator 更新 Secret 後，vmagent 要等 kubelet 同步（最多一分鐘左右）才重新載入。
# 不等就開始檢查的話測到的是舊設定——舊設定是對的時候，測試會假裝通過。實際發生過。
def config(path, timeout=180):
    with io.open(path, encoding="utf-8") as f:
        want = [rw.get("inlineUrlRelabelConfig") for rw in yaml.safe_load(f)["spec"]["remoteWrite"]]
    endpoint = "/api/v1/namespaces/route-test/app/vmagent-under-test:8429/proxy/remotewrite-url-relabel-config"
    deadline, got = time.time() + timeout, None
    while time.time() < deadline:
        try:
            got = [rw.get("relabel_config") for rw in (yaml.safe_load(kubectl("get", "--raw", endpoint)) or [])]
        except RuntimeError:
            got = None
        if got == want:
            print("vmagent 已載入要測的分流規則（%d 條 remoteWrite）" % len(want))
            return True
        time.sleep(5)
    print("等了 %d 秒，vmagent 載入的還不是要測的那份\n  要測的：%s\n  目前的：%s" % (timeout, want, got))
    return False


# ── labels：values.yaml → ArgoCD → namespace label ────────────────
def labels():
    ok = True
    print("%-20s %-44s %s" % ("namespace", "label（ArgoCD 貼的）", "結果"))
    for case, (obs, _) in CASES.items():
        # kubectl 預設不輸出 managedFields，要它才知道 label 是什麼時候、被誰貼的
        ns = json.loads(kubectl("get", "ns", namespace(case), "-o", "json", "--show-managed-fields"))
        got = {k: v for k, v in (ns["metadata"].get("labels") or {}).items() if k in OBS}
        want = {k: obs[k] for k in OBS if obs and obs.get(k)}
        problems = []
        if got != want:
            problems.append("預期 %s" % (" ".join("%s=%s" % kv for kv in sorted(want.items())) or "（沒有 label）"))
        if any(v == "" for v in got.values()):
            problems.append("有空字串的 label")
        # label 要在 pod 起來之前就貼好，不然最開始的資料會進 fallback
        if want:
            times = [m["time"] for m in ns["metadata"].get("managedFields", [])
                     if "f:labels" in json.dumps(m.get("fieldsV1", {})) and m.get("time")]
            pods = json.loads(kubectl("get", "pods", "-n", namespace(case), "-o", "json"))["items"]
            first_pod = min((p["metadata"]["creationTimestamp"] for p in pods), default=None)
            if not times:
                problems.append("找不到 label 是什麼時候貼的")
            elif first_pod and min(times) > first_pod:
                problems.append("label %s 才貼上，pod %s 就起來了" % (min(times), first_pod))
        shown = " ".join("%s=%s" % kv for kv in sorted(got.items())) or "（沒有）"
        print("%-20s %-44s %s" % (namespace(case), shown, "; ".join(problems) or "OK"))
        ok &= not problems
    return ok


# ── routing：namespace label → tenant ─────────────────────────────
def tenant_raw(tenant, path_and_query):
    return kubectl("get", "--raw",
                   "/api/v1/namespaces/route-test/app/vmsingle-%s:8429/proxy%s" % (tenant, path_and_query))


def routed():
    got = {namespace(c): set() for c in CASES}
    stored = {}
    for t in TENANTS:
        q = quote("count by (namespace, group) (sim_info)")
        for r in json.loads(tenant_raw(t, "/api/v1/query?query=" + q))["data"]["result"]:
            ns = r["metric"].get("namespace")
            if ns in got:
                got[ns].add(t)
                stored[(ns, t)] = r["metric"].get("group", "（沒有）")
    return got, stored


def export(tenant, start, end):
    """匯出原始樣本 {sensor: (timestamps, values)}。"""
    sel = quote('sim_sensor_value{namespace="%s"}' % namespace(SHARED))
    series = {}
    for line in tenant_raw(tenant, "/api/v1/export?match%%5B%%5D=%s&start=%d&end=%d" % (sel, start, end)).splitlines():
        if line.strip():
            d = json.loads(line)
            series[d["metric"].get("sensor")] = (d["timestamps"], d["values"])
    return series


def routing(wait=0):
    want = {namespace(c): tenants for c, (_, tenants) in CASES.items()}
    deadline = time.time() + wait
    while True:
        got, stored = routed()
        if got == want or time.time() >= deadline:
            break
        time.sleep(10)

    ok = True
    print("%-20s %-4s %-4s %-9s %s" % ("namespace", "IT", "IMC", "platform", "結果"))
    for ns, tenants in want.items():
        good = got[ns] == tenants
        ok &= good
        print("%-20s %-4s %-4s %-9s %s" % (ns, *["✓" if t in got[ns] else "·" for t in TENANTS],
                                             "OK" if good else "FAIL（預期 %s）" % ",".join(sorted(tenants))))

    bad = [(ns, t, g) for (ns, t), g in sorted(stored.items()) if g != t]
    print("\n存進 tenant 的 group 要等於 tenant 名稱：%s" % ("OK，%d 筆" % len(stored) if not bad else "FAIL"))
    for ns, t, g in bad:
        print("  %s 在 %s 裡是 group=%s" % (ns, t, g))
    ok &= not bad

    # 取一段兩邊都確定已經寫進去的區間，避開還在路上的樣本
    end = int(time.time()) - 15
    a, b = export("it", end - 90, end), export("imc", end - 90, end)
    n = sum(len(ts) for ts, _ in a.values())
    print("\nfan-out：%s 在 IT 和 IMC 的原始樣本" % namespace(SHARED))
    if not n:
        print("  FAIL  IT 裡沒有樣本，無法比對")
        ok = False
    elif a != b:
        print("  FAIL  兩邊不一樣：%s" % ", ".join(s for s in sorted(set(a) | set(b)) if a.get(s) != b.get(s)))
        ok = False
    else:
        print("  OK，%d 個感測器、%d 筆，timestamp（毫秒）和 value 完全相同" % (len(a), n))
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "config":
        ok = config(sys.argv[2])
    elif cmd == "labels":
        ok = labels()
    else:
        ok = routing(int(sys.argv[sys.argv.index("--wait") + 1]) if "--wait" in sys.argv else 0)
    print("\n" + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)

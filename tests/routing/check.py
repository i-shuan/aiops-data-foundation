"""查三個 tenant 各收到哪些 namespace，跟預期比對。

1. 分流：每個 tenant 查 count by (namespace, group) (sim_info)
   - 每個 namespace 出現在哪些 tenant，必須跟預期完全一樣（多一個、少一個都算錯）
   - 存進去的 group 必須等於那個 tenant 的名字
2. fan-out：shared 那個 namespace，匯出 IT 和 IMC 裡的原始樣本，
   每一筆的 timestamp 和 value 都要一樣。被抓兩次的話 timestamp 會不同，
   只有「同一次抓取複製成兩份」才會完全相同。

用法：python check.py [--wait 秒數]
全部對 exit 0，任何一項不對 exit 1。
"""
import json
import os
import subprocess
import sys
import time
from urllib.parse import quote

TENANTS = ["it", "imc", "platform"]

# namespace → 預期會出現在哪些 tenant
EXPECTED = {
    "zk-sim-it-prod":     {"it"},
    "zk-sim-imc-prod":    {"imc"},
    "zk-sim-shared-prod": {"it", "imc"},
    "zk-sim-none-prod":   {"platform"},   # namespace 沒有 label
    "zk-sim-bad-prod":    {"platform"},   # 有 label 但值不合法（模擬繞過 CI）
}
SHARED = "zk-sim-shared-prod"


def raw(tenant, path_and_query):
    path = "/api/v1/namespaces/route-test/services/vmsingle-%s:8429/proxy%s" % (tenant, path_and_query)
    env = dict(os.environ, MSYS_NO_PATHCONV="1")   # Git Bash 會把開頭是 / 的參數改成 Windows 路徑
    out = subprocess.run(["kubectl", "get", "--raw", path], capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError("查 %s 失敗：%s" % (tenant, out.stderr.strip()))
    return out.stdout


def routing():
    got = {ns: set() for ns in EXPECTED}
    stored = {}
    for t in TENANTS:
        q = quote("count by (namespace, group) (sim_info)")
        for r in json.loads(raw(t, "/api/v1/query?query=" + q))["data"]["result"]:
            ns = r["metric"].get("namespace")
            if ns in got:
                got[ns].add(t)
                stored[(ns, t)] = r["metric"].get("group", "(沒有)")
    return got, stored


def export(tenant, start, end):
    """匯出原始樣本。回傳 {sensor: (timestamps, values)}。"""
    sel = quote('sim_sensor_value{namespace="%s"}' % SHARED)
    out = raw(tenant, "/api/v1/export?match%%5B%%5D=%s&start=%d&end=%d" % (sel, start, end))
    series = {}
    for line in out.splitlines():
        if line.strip():
            d = json.loads(line)
            series[d["metric"].get("sensor")] = (d["timestamps"], d["values"])
    return series


def main():
    wait = int(sys.argv[sys.argv.index("--wait") + 1]) if "--wait" in sys.argv else 0
    deadline = time.time() + wait
    while True:
        got, stored = routing()
        if all(got[ns] == EXPECTED[ns] for ns in EXPECTED) or time.time() >= deadline:
            break
        time.sleep(10)

    ok = True
    print("%-20s %-4s %-4s %-9s %s" % ("namespace", "IT", "IMC", "platform", "結果"))
    for ns, want in EXPECTED.items():
        row = ["✓" if t in got[ns] else "·" for t in TENANTS]
        good = got[ns] == want
        ok &= good
        print("%-20s %-4s %-4s %-9s %s" % (ns, *row, "OK" if good else "FAIL（預期 %s）" % ",".join(sorted(want))))

    print("\n存進 tenant 的 group 值（應該等於 tenant 名稱）：")
    bad = [(ns, t, g) for (ns, t), g in sorted(stored.items()) if g != t]
    for ns, t, g in bad:
        print("  FAIL  %s 在 %s 裡是 group=%s" % (ns, t, g))
    if not bad:
        print("  OK，%d 筆全部相符" % len(stored))
    ok &= not bad

    print("\nfan-out：%s 在 IT 和 IMC 裡的原始樣本" % SHARED)
    # 取一段兩邊都確定已經寫進去的區間：結尾往前推，避開還在路上的樣本
    end = int(time.time()) - 15
    start = end - 90
    a, b = export("it", start, end), export("imc", start, end)
    n = sum(len(ts) for ts, _ in a.values())
    if not a or n == 0:
        print("  FAIL  IT 裡沒有樣本，無法比對")
        ok = False
    elif a != b:
        ok = False
        for sensor in sorted(set(a) | set(b)):
            if a.get(sensor) != b.get(sensor):
                print("  FAIL  感測器 %s 兩邊不一樣" % sensor)
    else:
        print("  OK，%d 個感測器、%d 筆原始樣本，timestamp 和 value 完全相同" % (len(a), n))

    print("\n" + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

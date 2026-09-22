"""查三個 tenant 各收到哪些 namespace，跟預期比對。

每個 tenant 各查一次 count by (namespace, group) (sim_info)：
  - 有沒有收到某個 namespace → 驗證分流
  - 存進去的 group 是不是那個 tenant 的名字 → 驗證 remoteWrite 的 relabel

用法：python check.py [--wait 秒數]
結果全對 exit 0，有任何一格不對 exit 1。
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
    "zk-sim-it-prod":   {"it"},
    "zk-sim-imc-prod":  {"imc"},
    "zk-sim-both-prod": {"it", "imc"},
    "zk-sim-none-prod": {"platform"},   # 沒登記
    "zk-sim-typo-prod": {"platform"},   # group 打錯字 → ns-labeler 拒標 → 兜底
    "zk-sim-plat-prod": {"platform"},   # it.platform → ns-labeler 拒標 → 兜底
}

QUERY = "count by (namespace, group) (sim_info)"


def query(tenant):
    path = "/api/v1/namespaces/route-test/services/vmsingle-%s:8429/proxy/api/v1/query?query=%s" % (
        tenant, quote(QUERY))
    env = dict(os.environ, MSYS_NO_PATHCONV="1")   # Git Bash 會把開頭是 / 的參數改成 Windows 路徑
    out = subprocess.run(["kubectl", "get", "--raw", path], capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError("查 %s 失敗：%s" % (tenant, out.stderr.strip()))
    return json.loads(out.stdout)["data"]["result"]


def collect():
    got = {ns: set() for ns in EXPECTED}
    stored_group = {}
    for t in TENANTS:
        for r in query(t):
            ns = r["metric"].get("namespace")
            if ns in got:
                got[ns].add(t)
                stored_group[(ns, t)] = r["metric"].get("group", "(沒有)")
    return got, stored_group


def main():
    wait = int(sys.argv[sys.argv.index("--wait") + 1]) if "--wait" in sys.argv else 0
    deadline = time.time() + wait
    while True:
        got, stored = collect()
        if all(got[ns] == EXPECTED[ns] for ns in EXPECTED) or time.time() >= deadline:
            break
        time.sleep(10)

    ok = True
    print("%-18s %-6s %-6s %-9s %s" % ("namespace", "IT", "IMC", "platform", "結果"))
    for ns, want in EXPECTED.items():
        row = []
        for t in TENANTS:
            mark = "✓" if t in got[ns] else "·"
            row.append(mark)
        good = got[ns] == want
        ok &= good
        why = "OK" if good else "FAIL（預期 %s）" % ",".join(sorted(want))
        print("%-18s %-6s %-6s %-9s %s" % (ns, *row, why))

    print()
    print("存進 tenant 的 group 值（應該等於 tenant 名稱）：")
    bad = [(ns, t, g) for (ns, t), g in sorted(stored.items()) if g != t]
    if bad:
        ok = False
        for ns, t, g in bad:
            print("  FAIL  %s 在 %s 裡是 group=%s" % (ns, t, g))
    else:
        print("  OK，%d 筆全部相符" % len(stored))

    print()
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

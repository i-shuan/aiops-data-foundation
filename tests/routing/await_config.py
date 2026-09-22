"""等到 vmagent 實際載入的分流設定，跟要測的那份一致為止。

改了 VMAgent 之後，operator 會更新設定用的 Secret，vmagent 要等 kubelet 同步掛載的 Secret
（最多一分鐘左右）才重新載入。在那之前它跑的還是舊設定。
不等就開始檢查的話，測到的是舊設定——而且舊設定是對的時候，測試會假裝通過。
實際發生過：regex 改壞了，測試卻 PASS。

用法：python await_config.py <gen_vmagent.py 產生的 yaml> [--timeout 秒數]
一致了 exit 0；逾時 exit 1，並印出目前載入的是什麼。
"""
import io
import os
import subprocess
import sys
import time

import yaml

ENDPOINT = "/api/v1/namespaces/route-test/services/vmagent-under-test:8429/proxy/remotewrite-url-relabel-config"


def loaded():
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    out = subprocess.run(["kubectl", "get", "--raw", ENDPOINT], capture_output=True, text=True, env=env)
    if out.returncode != 0:
        return None
    return [rw.get("relabel_config") for rw in (yaml.safe_load(out.stdout) or [])]


def main():
    path = sys.argv[1]
    timeout = int(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 180
    spec = yaml.safe_load(io.open(path, encoding="utf-8"))["spec"]
    want = [rw.get("inlineUrlRelabelConfig") for rw in spec["remoteWrite"]]

    deadline = time.time() + timeout
    got = None
    while time.time() < deadline:
        got = loaded()
        if got == want:
            print("vmagent 已載入要測的設定（%d 條 remoteWrite）" % len(want))
            return 0
        time.sleep(5)

    print("等了 %d 秒，vmagent 載入的設定還不是要測的那份" % timeout)
    print("要測的：%s" % want)
    print("目前的：%s" % got)
    return 1


if __name__ == "__main__":
    sys.exit(main())

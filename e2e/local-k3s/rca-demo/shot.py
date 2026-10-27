# 用 headless Edge／Chrome（DevTools Protocol）截 Grafana 儀表板。要 pip install websocket-client。
#   python shot.py <網址> <輸出.png> [等幾秒，預設 40] [高度，預設 1700]
# 內建瀏覽器背景時 Grafana 面板會一直 Loading；這支自己開一個瀏覽器、等面板載完再截。
# 時間範圍寫在網址：...&from=<開始 epoch ms>&to=now（事故還開著時，aiops-alerts 的 @timestamp 每分鐘更新，
# 結束時間寫死會把它排除在外，⑤⑥ 就會 No data）。
import base64, glob, json, os, subprocess, sys, tempfile, time, urllib.request

import websocket

url, out = sys.argv[1], sys.argv[2]
wait = int(sys.argv[3]) if len(sys.argv) > 3 else 40
height = int(sys.argv[4]) if len(sys.argv) > 4 else 1700
browsers = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe", "/usr/bin/chromium", "/usr/bin/google-chrome"]
exe = next((b for b in browsers if os.path.exists(b)), None) or sys.exit("找不到 Edge／Chrome")
port = 9333
prof = tempfile.mkdtemp(prefix="shot-")
p = subprocess.Popen([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--remote-debugging-port={port}",
                      "--remote-allow-origins=*", f"--window-size=1600,{height}", f"--user-data-dir={prof}", "about:blank"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(60):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
            break
        except Exception:
            time.sleep(0.3)
    ws = websocket.create_connection([t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"])
    n = 0

    def call(method, **params):
        global n
        n += 1
        ws.send(json.dumps({"id": n, "method": method, "params": params}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == n:
                return m.get("result", {})

    call("Emulation.setDeviceMetricsOverride", width=1600, height=height, deviceScaleFactor=1, mobile=False)
    call("Page.navigate", url=url)
    time.sleep(wait)
    open(out, "wb").write(base64.b64decode(call("Page.captureScreenshot", format="png")["data"]))
    print("saved", out)
finally:
    p.kill()

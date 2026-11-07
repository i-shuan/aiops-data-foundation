"""本機日誌磁碟寫滿：每 0.15 秒寫一筆約 2KB 的文件，共 400 筆；印每筆成功或失敗。run.sh journal 用。"""
import datetime
import sys
import time

sys.path[:0] = ["src", "."]
from common.infra.pg import DB
from common.infra.repos import Repos
from common.scenario import load_scenario

repo = Repos(None, load_scenario(), db=DB())
repo.state.ensure()
ok = fail = 0
for i in range(400):
    try:
        repo.state.set(f"j{i:03d}", {"i": i, "pad": "x" * 2000})
        ok += 1
        r = "ok"
    except Exception as e:  # noqa: BLE001
        fail += 1
        r = f"失敗({type(e).__name__})"
    print(datetime.datetime.now().strftime("%H:%M:%S"), f"j{i:03d}", r, flush=True)
    time.sleep(0.15)
print(f"SUMMARY ok={ok} fail={fail}", flush=True)

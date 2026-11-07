"""產生測試用的排程表（寫到 $1）：storm＝9 個工作同時開工（名字與逾時照真的排程，指令是只讀 PostgreSQL 的真指令 prune --dry-run）；
mem＝4 個真指令＋一個配 700MB 的 train 替身；daily＝每日工作的替身（retries 2）。真的 detect／rules／train 要 ES，這裡沒有，所以用替身。"""
import sys

import yaml

out = sys.argv[1]
real = ["main", "detector", "prune", "--config", "configs/baseline/prune.yaml", "--dry-run"]
job = lambda n, t, args=real, **kw: {"name": n, "stage": 1, "cron": "* * * * *", "timeout": t, "args": args, "why": "最緊資源測試", **kw}
TRAIN = ("import sys, time; sys.path.insert(0, 'src'); from common.infra.pg import DB; from common.infra.repos import Repos; "
         "from common.scenario import load_scenario; r = Repos(None, load_scenario(), db=DB()); b = bytearray(700 * 1024 * 1024); "
         "[b.__setitem__(i, 1) for i in range(0, len(b), 4096)]; r.state.set('train-%d' % int(time.time()), {'ok': 1}); time.sleep(10)")
cfgs = {
    "storm": [job(n, t) for n, t in (("rules", 50), ("detect", 50), ("alert", 600), ("trace-merge", 50), ("peer", 240),
                                     ("trace-collect", 240), ("trace-detect", 240), ("quality", 240), ("train", 3600))],
    "mem": [job("rules", 50), job("detect", 50), job("alert", 600), job("trace-merge", 50),
            job("train", 3600, ["tests._run_code", TRAIN], retries=2)],
    "daily": [job("train", 600, retries=2)],
}
for name, jobs in cfgs.items():
    open(f"{out}/{name}.yaml", "w").write(yaml.safe_dump({"timezone": "Asia/Taipei", "jobs": jobs}, allow_unicode=True))

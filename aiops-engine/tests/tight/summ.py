"""排程器的紀錄 → 每個工作 ok／失敗／逾時被砍／沒跑／跳過 各幾次，成功的花多久。run.sh 用。"""
import collections
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
res = collections.defaultdict(collections.Counter)
for ln in text.splitlines():
    m = re.match(r"\[scheduler\] (\S+) (\S+) (.*)", ln)
    if not m or m.group(1) in ("開始：", "收到停止：不再排新的，等還在跑的結束", "看守："):
        continue
    rest = m.group(3)
    k = ("ok" if rest.startswith("ok") else "逾時被砍" if "逾時" in rest else "失敗" if rest.startswith("失敗") else
         "沒跑(連不到DB)" if rest.startswith("沒跑") else "跳過" if rest.startswith("跳過") else None)
    if k:
        res[m.group(1)][k] += 1
for name, c in res.items():
    print(f"   {name:14}", "、".join(f"{k} {v}" for k, v in sorted(c.items())))
secs = [float(x) for x in re.findall(r" ok ([\d.]+)s", text)]
if secs:
    print(f"   成功 {len(secs)} 次，每次 {min(secs):.1f}～{max(secs):.1f} 秒")

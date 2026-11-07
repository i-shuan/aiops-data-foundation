"""prune：AIOps 自己寫的索引超過保留天數的文件每天清一次（configs/baseline/prune.yaml）。

aiops-anomalies、aiops-trace-segments、aiops-alerts 是一般索引（_id 固定、每輪覆蓋），不是 rollover 的資料流，
ILM 的 delete 會刪整個索引，所以照文件的時間 delete_by_query。人工判斷的 aiops-incidents、aiops-feedback、aiops-tuning
不在這裡：事故紀錄要長留，掉了不能重算（備份見 elasticsearch/kibana-dev-tools.txt 第 5 段）。
"""
from __future__ import annotations

import pandas as pd

from common.clock import now_of
from detect.config.prune import PruneConfig
from common.infra.es import ES
from common.infra.repos import Repos
from common.scenario import load_scenario


def plan(prune_config: PruneConfig, now: pd.Timestamp) -> list[dict]:
    """每個索引要刪什麼：{"index", "query", "keep_days", "time_field"}。
    query：time_field < now − keep_days，再加上設定的 filter（例如 aiops-alerts 只刪 status resolved）。"""
    out = []
    for it in prune_config.indices:
        days = float(it.keep_days)
        cutoff = (now - pd.Timedelta(days=days)).isoformat()
        tf = it.time_field
        must = [{"range": {tf: {"lt": cutoff}}}]
        if it.filter:
            must.append(it.filter)
        out.append({"index": it.index, "keep_days": days, "time_field": tf, "cutoff": cutoff,
                    "query": {"bool": {"filter": must}}})
    return out


def cmd_prune(prune_config: PruneConfig, args) -> int:
    hk = Repos(ES(), load_scenario()).housekeeping
    now = now_of(args.at)
    dry = bool(getattr(args, "dry_run", False))
    total = 0
    for p in plan(prune_config, now):
        n = hk.count(p["index"], p["query"])
        if dry or not n:
            print(f"  {p['index']}：{p['time_field']} 早於 {p['cutoff'][:16]}（保留 {p['keep_days']:g} 天）的有 {n} 筆" + ("，沒刪（--dry-run）" if dry and n else ""))
            total += n
            continue
        r = hk.delete_by_query(p["index"], p["query"])
        deleted = int(r.get("deleted", 0))
        total += deleted
        print(f"  {p['index']}：刪了 {deleted} 筆（{p['time_field']} 早於 {p['cutoff'][:16]}，保留 {p['keep_days']:g} 天）"
              + (f"；{len(r['failures'])} 筆失敗" if r.get("failures") else ""))
    print(f"{now.tz_convert(prune_config.timezone):%m-%d %H:%M}：共 {total} 筆" + ("要刪" if dry else ""))
    return 0

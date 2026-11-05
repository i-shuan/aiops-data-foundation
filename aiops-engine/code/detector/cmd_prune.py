"""prune：AIOps 自己寫的索引超過保留天數的文件每天清一次（configs/baseline/prune.yaml）。

aiops-anomalies、aiops-trace-segments、aiops-alerts 是一般索引（_id 固定、每輪覆蓋），不是 rollover 的資料流，
ILM 的 delete 會刪整個索引，所以照文件的時間 delete_by_query。人工判斷的 aiops-incidents、aiops-feedback、aiops-tuning
不在這裡：事故紀錄要長留，掉了不能重算（備份見 elasticsearch/kibana-dev-tools.txt 第 5 段）。
"""
from __future__ import annotations

import pandas as pd

from .es import ES
from .common import _now


def plan(cfg: dict, now: pd.Timestamp) -> list[dict]:
    """每個索引要刪什麼：{"index", "query", "keep_days", "time_field"}。
    query：time_field < now − keep_days，再加上設定的 filter（例如 aiops-alerts 只刪 status resolved）。"""
    out = []
    for it in cfg.get("indices", []):
        days = float(it["keep_days"])
        cutoff = (now - pd.Timedelta(days=days)).isoformat()
        tf = it.get("time_field", "@timestamp")
        must = [{"range": {tf: {"lt": cutoff}}}]
        if it.get("filter"):
            must.append(it["filter"])
        out.append({"index": it["index"], "keep_days": days, "time_field": tf, "cutoff": cutoff,
                    "query": {"bool": {"filter": must}}})
    return out


def cmd_prune(cfg: dict, args) -> int:
    es = ES()
    now = _now(args.at)
    dry = bool(getattr(args, "dry_run", False))
    total = 0
    for p in plan(cfg, now):
        q = {"query": p["query"]}
        n = es.req("POST", f"/{p['index']}/_count?ignore_unavailable=true&allow_no_indices=true", q).get("count", 0)
        if dry or not n:
            print(f"  {p['index']}：{p['time_field']} 早於 {p['cutoff'][:16]}（保留 {p['keep_days']:g} 天）的有 {n} 筆" + ("，沒刪（--dry-run）" if dry and n else ""))
            total += n
            continue
        r = es.req("POST", f"/{p['index']}/_delete_by_query?conflicts=proceed&ignore_unavailable=true&allow_no_indices=true"
                           f"&slices=auto&wait_for_completion=true&refresh=true", q)   # refresh：刪完馬上查得到，下一輪不會重算
        deleted = int(r.get("deleted", 0))
        total += deleted
        print(f"  {p['index']}：刪了 {deleted} 筆（{p['time_field']} 早於 {p['cutoff'][:16]}，保留 {p['keep_days']:g} 天）"
              + (f"；{len(r['failures'])} 筆失敗" if r.get("failures") else ""))
    print(f"{now.tz_convert(cfg.get('timezone', 'Asia/Taipei')):%m-%d %H:%M}：共 {total} 筆" + ("要刪" if dry else ""))
    return 0

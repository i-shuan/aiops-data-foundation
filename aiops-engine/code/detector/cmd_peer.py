"""跟兄弟比：同一個服務的 Pod 互相比（detector/peer.py、configs/baseline/peer.yaml）。"""
from __future__ import annotations

import pandas as pd

from .es import ANOMALY_INDEX, ANOMALY_MAPPING, ES, doc_id, fetch
from . import peer as P
from .common import RULE_FIELDS, _freq, _now, keys_of
from .owner import write_anomalies


SHOW = {"pct": lambda x: f"{x:.0%}", "seconds": lambda x: f"{x * 1000:.0f}ms", "cores": lambda x: f"{x:.2f} 核",
        "bytes": lambda x: f"{x / 1048576:.0f}MB"}

def cmd_peer(cfg: dict, args) -> int:
    es = ES()
    es.ensure_index(ANOMALY_INDEX, ANOMALY_MAPPING)
    es.req("PUT", f"/{ANOMALY_INDEX}/_mapping", {"properties": RULE_FIELDS})
    end = _now(args.at)
    start = end - pd.Timedelta(_freq(cfg.get("window", "10m")))
    names = set(args.only.split(",")) if args.only else None
    warmup = pd.Timedelta(_freq(cfg["warmup"])) if cfg.get("warmup") else None
    if warmup is not None and warmup >= end - start:
        raise SystemExit(f"warmup（{cfg['warmup']}）要比 window 短")
    docs, fired, checked = [], 0, 0
    for c in cfg["checks"]:
        if names and c["name"] not in names:
            continue
        checked += 1
        keys = keys_of(c)
        group, member = keys[:-1], keys[-1]
        cur = fetch(es, c, start, end, None)
        # 只比現在還在的 Pod：最近 fresh（預設 2 分鐘）內還有資料的。換版、縮容刪掉的 Pod 不算兄弟
        alive = fetch(es, P.count_spec(c), end - pd.Timedelta(_freq(cfg.get("fresh", "2m"))), end, None)
        cur = cur.merge(alive[keys], on=keys) if not cur.empty and not alive.empty else cur.iloc[0:0]
        # 剛起來的 Pod 不比（warmup）：起來前幾分鐘 JIT、載入、heap 還在長，CPU 偏高、記憶體偏低，會誤報自己或舊 Pod。
        # 在 window 開頭到 end - warmup 之間就有資料的，才算暖機完
        n_young = 0
        if warmup is not None and not cur.empty:
            old = fetch(es, P.count_spec(c), start, end - warmup, None)
            before = len(cur)
            cur = cur.merge(old[keys], on=keys) if not old.empty else cur.iloc[0:0]
            n_young = before - len(cur)
        if c.get("min_volume") and (vs := P.volume_spec(c)) is not None and not cur.empty:
            vol = fetch(es, vs, start, end, None).rename(columns={"value": "volume"})
            cur = cur.merge(vol[keys + ["volume"]], on=keys, how="left")
            cur = cur[cur["volume"].fillna(0) >= c["min_volume"]]
        res = P.compare(cur, group, member, direction=c.get("direction", "up"), z=cfg.get("z", 5.0),
                        min_abs=c.get("min_abs", 0.0), min_ratio=c.get("min_ratio"),
                        rel_floor=cfg.get("rel_floor", 0.1), min_peers=cfg.get("min_peers", 3),
                        max_peer_median=c.get("max_peer_median"),
                        z_by_peers={int(k): float(v) for k, v in (cfg.get("z_by_peers") or {}).items()})
        fmt = SHOW.get(c.get("show"), lambda x: f"{x:g}")
        n_groups = res.groupby(group).ngroups if not res.empty else 0
        for r in res[res["anomaly"]].to_dict("records"):
            k = {f: r[f] for f in keys}
            msg = P.describe(c, r, keys, fmt)
            docs.append((doc_id(c["name"], k, end.floor("1min")), {
                "@timestamp": end.floor("1min").isoformat(), "series": c["name"], "rule": c["name"], "keys": k,
                "value": r["value"], "median": r["median"], "expected_low": None, "expected_high": None, "z": r["z"],
                "level": "peer", "direction": c.get("direction", "up"), "severity": c.get("severity", "warning"),
                "message": msg, "detected_at": pd.Timestamp.now(tz="UTC").isoformat()}))
            print(f"  [{c.get('severity', 'warning')}] {msg}")
            fired += 1
        print(f"  {c['name']}：{n_groups} 組、{len(res)} 個 Pod 比過" + (f"（{n_young} 個 Pod 暖機中不比）" if n_young else ""))
    if docs:
        write_anomalies(es, docs)
    print(f"{end.tz_convert('Asia/Taipei'):%m-%d %H:%M}：{checked} 個檢查，觸發 {fired} 個")
    return 0

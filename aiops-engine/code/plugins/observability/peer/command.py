"""detector peer：Pod 同儕橫向比對，同一個服務的 Pod 互相比（peer.py 算、這裡接 Detector 框架與 typer；configs/baseline/peer.yaml）。
IT 專用，由 observability plugin 的 commands() 掛上。"""
from __future__ import annotations

import pandas as pd

from core.anomaly import Anomaly
from core.timeutil import _freq, keys_of
from detector.cli import Opt, run
from detector.detector import Detector
from sources import fetch

from . import peer as P
from .config import PeerConfig


SHOW = {"pct": lambda x: f"{x:.0%}", "seconds": lambda x: f"{x * 1000:.0f}ms", "cores": lambda x: f"{x:.2f} 核",
        "bytes": lambda x: f"{x / 1048576:.0f}MB"}


class PeerDetector(Detector):
    level = "peer"

    def stamp(self) -> pd.Timestamp:
        return self.now.floor("1min")

    def checks(self) -> list[dict]:
        cfg, end = self.cfg, self.now
        self.start = end - pd.Timedelta(_freq(cfg.get("window", "10m")))
        self.warmup = pd.Timedelta(_freq(cfg["warmup"])) if cfg.get("warmup") else None
        if self.warmup is not None and self.warmup >= end - self.start:
            raise SystemExit(f"warmup（{cfg['warmup']}）要比 window 短")
        return cfg["checks"]

    def evaluate(self, c: dict) -> list[Anomaly]:
        cfg, es, start, end, warmup = self.cfg, self.es, self.start, self.now, self.warmup
        keys = keys_of(c)
        group, member = keys[:-1], keys[-1]
        cur = fetch(es, c, start, end, None)
        # 只比現在還在的 Pod：最近 fresh（預設 2 分鐘）內還有資料的。換版、縮容刪掉的 Pod 不算同儕
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
        self.note = f"  {c['name']}：{n_groups} 組、{len(res)} 個 Pod 比過" + (f"（{n_young} 個 Pod 暖機中不比）" if n_young else "")
        return [Anomaly(ts=self.slot, series=c["name"], rule=c["name"], keys={f: r[f] for f in keys}, value=r["value"],
                        median=r["median"], z=r["z"], level=self.level, direction=c.get("direction", "up"),
                        severity=c.get("severity", "warning"), message=P.describe(c, r, keys, fmt))
                for r in res[res["anomaly"]].to_dict("records")]

    def after_check(self, c: dict, hits: list[Anomaly]) -> None:
        print(self.note)


def cmd_peer(cfg: dict, args, backend=None) -> int:
    return PeerDetector(cfg, args, backend).run()


def peer(config: str = Opt(...), at: str | None = Opt(None, help="回測：拿過去某個時間點當成現在"),
         only: str | None = Opt(None, help="只跑這幾個（逗號分開）")) -> int:
    return run(cmd_peer, PeerConfig, locals())


def register(app) -> None:
    app.command("peer")(peer)

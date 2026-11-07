"""跟兄弟比（同儕比較）：同一個服務的幾個 Pod 互相比，抓「只有一個 Pod 壞掉」（docs/metrics/README.md 的做法 4）。

為什麼要：10 個 Pod 裡 1 個錯誤率 100%，整個服務的錯誤率只有 10%，跟平常比（baseline）可能還在範圍裡；
記憶體只有一個 Pod 一直漲，平均起來也看不出來。同一個服務的 Pod 跑同一份程式、分到差不多的流量，
應該長得差不多，所以拿「其他 Pod」當標準，不用歷史，裝上去第一天就能用。

每個檢查（configs/baseline/peer.yaml）：
  1. 用 series.yaml 一樣的寫法查出每個 Pod 最近 window（例如 10 分鐘）的一個值（錯誤率、p95、CPU…）。
  2. 依 group（例如服務）分組，至少 min_peers 個 Pod 才比。
  3. 每個 Pod 跟「其他 Pod」比：其他 Pod 的中位數、MAD（跟 baseline 一樣的穩健統計，少數怪的不會把標準拉偏），
     離中位數 z 倍穩健標準差以上、而且差距 ≥ min_abs（以及 ≥ min_ratio 倍，若有設）才算。
     其他 Pod 只有 2～4 個時，它們之間的差距沒什麼統計意義（滾動更新後新舊 Pod 的記憶體本來就差幾十 MB，會把分母撐大），
     z 的門檻依其他 Pod 數分級（z_by_peers，例如其他 2 個用 3），主要靠 min_abs、min_ratio 擋誤報
     （docs/pdca/phase2-anomaly/2026-11-peer-z-and-slope.md）。
  流量太少的 Pod（min_volume，例如 10 分鐘不到 20 個請求）不比：錯誤率一兩個錯就跳很高。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from detect.strategy.dynamic_baseline import MAD_TO_SIGMA
from detect.store.models import Anomaly
from shared.config.peer import PeerCheck, PeerConfig
from detect.base import Detector
from detect.utils import _freq, keys_of


def compare(df: pd.DataFrame, group: list[str], member: str, *, direction: str = "up", z: float = 5.0,
            min_abs: float = 0.0, min_ratio: float | None = None, rel_floor: float = 0.1, abs_floor: float = 1e-9,
            min_peers: int = 3, max_peer_median: float | None = None, z_by_peers: dict | None = None) -> pd.DataFrame:
    """df：group + [member, "value"]，每個 Pod 一列。

    回傳每個 Pod 一列：group、member、value、peers（其他 Pod 幾個）、median（其他 Pod 的中位數）、z、anomaly。
    rel_floor：其他 Pod 幾乎一樣時（MAD≈0），差距至少當成中位數的 rel_floor 倍，避免一點點不同就算。
    z_by_peers：{其他 Pod 數: z 門檻}，沒列的用 z（例如 {2: 3, 3: 4, 4: 4}：其他只有 2 個時 3 倍就算）。
    max_peer_median：其他 Pod 的中位數已經到這個值（例如錯誤率 5%）＝整個服務都壞了，不挑出單一個 Pod，交給規則。
    """
    cols = group + [member, "value", "peers", "median", "z", "anomaly"]
    d = df.dropna(subset=["value"])
    rows = []
    for _, g in d.groupby(group, sort=False, dropna=False):
        if len(g) < max(min_peers, 2):
            continue
        vals = g["value"].to_numpy(dtype=float)
        zt = float((z_by_peers or {}).get(len(vals) - 1, z))   # 其他 Pod 數 = 這組減自己
        for i, r in enumerate(g.to_dict("records")):
            others = np.delete(vals, i)
            med = float(np.median(others))
            mad = float(np.median(np.abs(others - med)))
            sigma = max(MAD_TO_SIGMA * mad, rel_floor * abs(med), abs_floor)
            diff = r["value"] - med
            zz = diff / sigma
            if max_peer_median is not None and direction == "up" and med >= max_peer_median:
                hit = False
            elif direction == "up":
                hit = zz >= zt and diff >= min_abs and (min_ratio is None or r["value"] >= min_ratio * med)
            elif direction == "down":
                hit = zz <= -zt and -diff >= min_abs and (min_ratio is None or r["value"] * min_ratio <= med)
            else:
                hit = abs(zz) >= zt and abs(diff) >= min_abs
            rows.append({**{k: r[k] for k in group}, member: r[member], "value": r["value"],
                         "peers": len(others), "median": med, "z": zz, "anomaly": bool(hit)})
    return pd.DataFrame(rows, columns=cols)


def volume_spec(spec: dict) -> dict | None:
    """錯誤率、p95 這種從直方圖算的，另外查每個 Pod 的請求數（同一個直方圖的次數），用來排除流量太少的 Pod。"""
    def one(v: dict) -> dict | None:
        (kind, arg), = v["value"].items() if isinstance(v["value"], dict) else ((None, None),)
        if kind in ("hist_ratio", "percentile"):
            return {**v, "value": {"hist_count": arg["field"]}}
        return None
    return one(spec)


def count_spec(spec: dict) -> dict:
    """同一個查詢、只數筆數：看哪些 Pod 最近還有資料。"""
    return {**spec, "value": "count"}


def describe(check: PeerCheck, r: dict, keys: list[str], fmt) -> str:
    who = "、".join(str(r[k]) for k in keys if r.get(k) is not None)
    return (f"{check.title or check.name}：{who}（{fmt(r['value'])}，其他 {r['peers']} 個 Pod 中位數 {fmt(r['median'])}，"
            f"z={'>999' if abs(r['z']) > 999 else format(r['z'], '.1f')}）")   # 其他 Pod 都是 0 時 z 會上億，沒意義


SHOW = {"pct": lambda x: f"{x:.0%}", "seconds": lambda x: f"{x * 1000:.0f}ms", "cores": lambda x: f"{x:.2f} 核",
        "bytes": lambda x: f"{x / 1048576:.0f}MB"}


class PeerDetector(Detector):
    level = "peer"

    def stamp(self) -> pd.Timestamp:
        return self.now.floor("1min")

    def checks(self) -> list[PeerCheck]:
        peer_config, end = self.config, self.now
        self.start = end - pd.Timedelta(_freq(peer_config.window))
        self.warmup = pd.Timedelta(_freq(peer_config.warmup)) if peer_config.warmup else None
        if self.warmup is not None and self.warmup >= end - self.start:
            raise SystemExit(f"warmup（{peer_config.warmup}）要比 window 短")
        return peer_config.checks

    def evaluate(self, c: PeerCheck) -> list[Anomaly]:
        peer_config, fetch, start, end, warmup = self.config, self.repo.series.fetch, self.start, self.now, self.warmup
        keys = keys_of(c)
        group, member = keys[:-1], keys[-1]
        cur = fetch(c, start, end, None)
        # 只比現在還在的 Pod：最近 fresh（預設 2 分鐘）內還有資料的。換版、縮容刪掉的 Pod 不算同儕
        alive = fetch(count_spec(c), end - pd.Timedelta(_freq(peer_config.fresh)), end, None)
        cur = cur.merge(alive[keys], on=keys) if not cur.empty and not alive.empty else cur.iloc[0:0]
        # 剛起來的 Pod 不比（warmup）：起來前幾分鐘 JIT、載入、heap 還在長，CPU 偏高、記憶體偏低，會誤報自己或舊 Pod。
        # 在 window 開頭到 end - warmup 之間就有資料的，才算暖機完
        n_young = 0
        if warmup is not None and not cur.empty:
            old = fetch(count_spec(c), start, end - warmup, None)
            before = len(cur)
            cur = cur.merge(old[keys], on=keys) if not old.empty else cur.iloc[0:0]
            n_young = before - len(cur)
        if c.min_volume and (vs := volume_spec(c)) is not None and not cur.empty:
            vol = fetch(vs, start, end, None).rename(columns={"value": "volume"})
            cur = cur.merge(vol[keys + ["volume"]], on=keys, how="left")
            cur = cur[cur["volume"].fillna(0) >= c.min_volume]
        res = compare(cur, group, member, direction=c.direction, z=peer_config.z,
                        min_abs=c.min_abs, min_ratio=c.min_ratio,
                        rel_floor=peer_config.rel_floor, min_peers=peer_config.min_peers,
                        max_peer_median=c.max_peer_median,
                        z_by_peers={int(k): float(v) for k, v in peer_config.z_by_peers.items()})
        fmt = SHOW.get(c.show, lambda x: f"{x:g}")
        n_groups = res.groupby(group).ngroups if not res.empty else 0
        self.note = f"  {c.name}：{n_groups} 組、{len(res)} 個 Pod 比過" + (f"（{n_young} 個 Pod 暖機中不比）" if n_young else "")
        return [Anomaly(ts=self.slot, series=c.name, rule=c.name, keys={f: r[f] for f in keys}, value=r["value"],
                        median=r["median"], z=r["z"], level=self.level, direction=c.direction,
                        severity=c.severity, message=describe(c, r, keys, fmt))
                for r in res[res["anomaly"]].to_dict("records")]

    def after_check(self, c: PeerCheck, hits: list[Anomaly]) -> None:
        print(self.note)


def cmd_peer(peer_config: PeerConfig, args, repo=None) -> int:
    return PeerDetector(peer_config, args, repo).run()

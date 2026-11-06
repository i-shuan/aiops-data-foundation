"""跟兄弟比（detector/peer.py）的單元測試，不需要 ES。"""
import contextlib
import io
import unittest

from tests.fakes import MemoryRepos
from unittest import mock

import pandas as pd
import yaml

import detect.strategy.peer_compare as P


def pods(values, svc="order-api", **extra):
    return pd.DataFrame([{"service": svc, "pod": f"{svc}-{i}", "value": v, **extra} for i, v in enumerate(values)])


def hits(res):
    return list(res[res["anomaly"]]["pod"])


class CompareTest(unittest.TestCase):
    def test_one_pod_errors_others_fine(self):
        # 10 個 Pod，只有一個錯誤率 100%：整個服務平均才 10%，但跟兄弟比一眼看出來
        res = P.compare(pods([0.0] * 9 + [1.0]), ["service"], "pod", min_abs=0.05)
        self.assertEqual(hits(res), ["order-api-9"])
        r = res[res["pod"] == "order-api-9"].iloc[0]
        self.assertEqual((r["peers"], r["median"]), (9, 0.0))

    def test_all_pods_bad_is_not_a_peer_problem(self):
        # 全部一起壞（例如下游 DB 掛了）：不是「某個 Pod」的問題，交給 baseline
        self.assertEqual(hits(P.compare(pods([0.9, 0.95, 1.0, 0.92]), ["service"], "pod", min_abs=0.05)), [])

    def test_normal_spread_not_reported(self):
        # Pod 之間本來就有點差：p95 120～180ms
        res = P.compare(pods([0.12, 0.15, 0.18, 0.14, 0.16]), ["service"], "pod", min_abs=0.1, min_ratio=2)
        self.assertEqual(hits(res), [])

    def test_slow_pod_needs_both_abs_and_ratio(self):
        base = [0.010, 0.011, 0.012, 0.010]
        # 10ms → 60ms：倍數夠，但只慢 50ms，不到 100ms
        self.assertEqual(hits(P.compare(pods(base + [0.06]), ["service"], "pod", min_abs=0.1, min_ratio=2)), [])
        # 10ms → 1.5 秒：兩個都過
        self.assertEqual(hits(P.compare(pods(base + [1.5]), ["service"], "pod", min_abs=0.1, min_ratio=2)),
                         ["order-api-4"])

    def test_needs_min_peers(self):
        # 只有 2 個 Pod：分不出誰才是怪的
        self.assertEqual(len(P.compare(pods([0.0, 1.0]), ["service"], "pod", min_abs=0.05)), 0)
        self.assertEqual(hits(P.compare(pods([0.0, 0.0, 1.0]), ["service"], "pod", min_abs=0.05)), ["order-api-2"])

    def test_groups_are_separate(self):
        # 不同服務不互比：report 本來就比 order 慢
        df = pd.concat([pods([0.01, 0.01, 0.01], svc="order"), pods([2.0, 2.1, 1.9], svc="report")])
        self.assertEqual(hits(P.compare(df, ["service"], "pod", min_abs=0.1, min_ratio=2)), [])

    def test_lower_is_fine_when_direction_up(self):
        self.assertEqual(hits(P.compare(pods([0.5, 0.5, 0.5, 0.0]), ["service"], "pod", min_abs=0.05)), [])

    def test_all_bad_uneven_not_singled_out(self):
        # 3 個都壞、其中一個更嚴重（40%、45%、100%）：沒有 max_peer_median 時只報最嚴重那個，好像只有它壞
        vals = [0.40, 0.45, 1.0]
        self.assertEqual(hits(P.compare(pods(vals), ["service"], "pod", min_abs=0.05)), ["order-api-2"])
        # 其他 Pod 的中位數已經 ≥ 5%（規則的錯誤率門檻）：整個服務都壞了，不挑單一個
        self.assertEqual(hits(P.compare(pods(vals), ["service"], "pod", min_abs=0.05, max_peer_median=0.05)), [])
        # 其他 Pod 正常時照樣抓
        self.assertEqual(hits(P.compare(pods([0.0, 0.01, 1.0]), ["service"], "pod", min_abs=0.05, max_peer_median=0.05)),
                         ["order-api-2"])

    def test_three_pods_different_ages_leak(self):
        # 2026-10-31 真的 cluster 上：3 個 Pod 一個洩漏到 420MB，另兩個 251MB（舊）、163MB（滾動更新後的新 Pod）。
        # 多 213MB、2.03 倍兩道都過，但其他 2 個之間差 88MB 把分母撐大，z 只有 3.3：用 5 抓不到，其他 2 個時門檻 3 才抓到
        MB = 1048576
        vals = [420 * MB, 251 * MB, 163 * MB]
        kw = dict(min_abs=100 * MB, min_ratio=1.5)
        self.assertEqual(hits(P.compare(pods(vals), ["service"], "pod", **kw)), [])
        res = P.compare(pods(vals), ["service"], "pod", z_by_peers={2: 3, 3: 4, 4: 4}, **kw)
        self.assertEqual(hits(res), ["order-api-0"])
        self.assertAlmostEqual(float(res[res["pod"] == "order-api-0"]["z"].iloc[0]), 3.3, places=1)
        # 門檻放低但兩道硬門檻還在：差 90MB（<100MB）、1.4 倍（<1.5）的不報
        self.assertEqual(hits(P.compare(pods([297 * MB, 251 * MB, 163 * MB]), ["service"], "pod", z_by_peers={2: 3}, **kw)), [])
        # 其他 Pod 有 4 個：沒列在 z_by_peers 就用 z=5，同樣的差距（z 3.3）不報
        self.assertEqual(hits(P.compare(pods([420 * MB, 251 * MB, 163 * MB, 251 * MB, 163 * MB]), ["service"], "pod",
                                        z_by_peers={2: 3}, **kw)), [])

    def test_majority_bad(self):
        # 3 個壞 2 個：其他 Pod 的中位數本身就壞了，分不出；5 個壞 2 個：兩個都抓
        self.assertEqual(hits(P.compare(pods([0.0, 0.3, 0.3]), ["service"], "pod", min_abs=0.05)), [])
        self.assertEqual(hits(P.compare(pods([0.0, 0.3, 0.0, 0.3, 0.0]), ["service"], "pod", min_abs=0.05)),
                         ["order-api-1", "order-api-3"])


class CmdPeerTest(unittest.TestCase):
    """cmd_peer 的 fresh（還在的）、warmup（暖機完的）篩選：用假的 fetch，不需要 ES。"""

    END = pd.Timestamp("2026-10-28T12:00:00Z")

    def run_peer(self, life, values, cfg_extra=None):
        """life：Pod → (起來, 停掉) 相對 END 的分鐘數（停掉 None＝還在）；values：Pod → CPU。回傳被報的 Pod。"""
        import detect.base as B
        import detect.strategy.peer_compare as C

        def fetch(spec, start, end, interval):
            if spec["value"] == "count":   # 這段時間有資料的 Pod
                rows = [p for p, (b, d) in life.items()
                        if self.END + pd.Timedelta(minutes=b) < end and (d is None or self.END + pd.Timedelta(minutes=d) > start)]
                return pd.DataFrame({"ns": ["n"] * len(rows), "deploy": ["d"] * len(rows), "pod": rows, "value": 1.0})
            rows = [p for p in values if p in life]
            return pd.DataFrame({"ns": ["n"] * len(rows), "deploy": ["d"] * len(rows), "pod": rows,
                                 "value": [values[p] for p in rows]})

        written = []
        repo = MemoryRepos()
        repo.series.fetch = fetch
        cfg = {"window": "10m", "fresh": "2m", "min_peers": 3, **(cfg_extra or {}), "checks": [
            {"name": "peer.pod_cpu", "title": "CPU 高", "index": "x", "by": ["ns", "deploy", "pod"],
             "value": {"avg": "cpu"}, "direction": "up", "min_abs": 0.2, "min_ratio": 3}]}
        args = type("A", (), {"at": self.END.isoformat(), "only": None})()
        # 開連線、寫異常在 Detector 基底（detector/base.py）；查資料在 cmd_peer 自己
        with contextlib.redirect_stdout(io.StringIO()):
            C.cmd_peer(cfg, args, repo)
        return sorted(d["keys"]["pod"] for _, d in repo.docs("aiops-anomalies"))

    def test_young_pod_not_compared(self):
        # 3 個舊的＋1 個 1 分鐘前剛起來（CPU 還是 1 核）：沒有 warmup 會報新的；warmup 5m 不報
        life = {"a": (-60, None), "b": (-60, None), "c": (-60, None), "new": (-1, None)}
        cpu = {"a": 0.2, "b": 0.2, "c": 0.2, "new": 1.0}
        self.assertEqual(self.run_peer(life, cpu), ["new"])
        self.assertEqual(self.run_peer(life, cpu, {"warmup": "5m"}), [])
        # 6 分鐘後（還是 1 核）：暖機完了，照樣抓
        life["new"] = (-6, None)
        self.assertEqual(self.run_peer(life, cpu, {"warmup": "5m"}), ["new"])

    def test_gone_pod_not_compared(self):
        # b 3 分鐘前就沒資料了（被換掉）：不算兄弟，剩 a、c、d
        life = {"a": (-60, None), "b": (-60, -3), "c": (-60, None), "d": (-60, None)}
        self.assertEqual(self.run_peer(life, {"a": 0.2, "b": 1.0, "c": 0.2, "d": 0.2}, {"warmup": "5m"}), [])

    def test_warmup_must_be_shorter_than_window(self):
        with self.assertRaises(SystemExit):
            self.run_peer({"a": (-60, None)}, {"a": 0.2}, {"warmup": "10m"})


class DescribeTest(unittest.TestCase):
    def test_huge_z_capped(self):
        r = {"service": "order-api", "pod": "order-api-9", "value": 0.13, "peers": 2, "median": 0.0, "z": 1.3e8}
        msg = P.describe({"title": "錯誤率比兄弟高"}, r, ["service", "pod"], lambda x: f"{x:.0%}")
        self.assertEqual(msg, "錯誤率比兄弟高：order-api、order-api-9（13%，其他 2 個 Pod 中位數 0%，z=>999）")


class SpecTest(unittest.TestCase):
    def test_volume_spec_from_histogram(self):
        with open("configs/baseline/peer.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        by = {c["name"]: c for c in cfg["checks"]}
        v = P.volume_spec(by["peer.http_error_rate"])
        self.assertEqual(v["value"], {"hist_count": "metrics.http.server.request.duration"})
        self.assertIsNone(P.volume_spec(by["peer.pod_cpu"]))   # CPU 不用看請求數
        self.assertEqual(P.count_spec(by["peer.pod_cpu"])["value"], "count")


if __name__ == "__main__":
    unittest.main()

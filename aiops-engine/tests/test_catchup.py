"""detect 的水位線補算（detect/detect_service.catchup_plan；docs/pdca/meta/2026-11-pg-resilience.md）。為了確認：
1. 照常每分鐘跑：沒有空白、不補。
2. 停過（上一輪只看到 covered_until，比這一輪的開頭早）：空白記下來，每輪補一段 step，補完就沒了；每段接著上一段、不漏不重。
3. 空白比 max 還久：只補最近 max；max 0s 不補。
4. cmd_detect 真的多判一段，而且 --at 回放不動進度。"""
import unittest
from argparse import Namespace
from unittest import mock

import pandas as pd

import detect.detect_service as D

T = pd.Timestamp("2026-11-07T10:00:00Z")
H = pd.Timedelta(hours=1)
STEP, MAX = pd.Timedelta(minutes=30), pd.Timedelta(hours=6)


class Plan(unittest.TestCase):
    def test_no_gap_when_running_normally(self):
        rest, chunk = D.catchup_plan({"covered_until": T.isoformat()}, T - pd.Timedelta(minutes=10), MAX, STEP)
        self.assertEqual((rest, chunk), ({}, None))
        self.assertEqual(D.catchup_plan(None, T, MAX, STEP), ({}, None))                 # 第一次跑：沒有進度，不補

    def test_gap_is_filled_step_by_step_without_holes(self):
        start = T                                                                         # 這一輪看 [T, T+10m)
        state = {"covered_until": (T - 2 * H).isoformat()}                                # 停了 2 小時
        chunks = []
        for i in range(10):
            rest, chunk = D.catchup_plan(state, start + i * pd.Timedelta(minutes=1), MAX, STEP)
            if chunk is None:
                break
            chunks.append((chunk[0] - chunk[1], chunk[0]))
            state = {"covered_until": (start + i * pd.Timedelta(minutes=1) + pd.Timedelta(minutes=10)).isoformat(), **rest}
        self.assertEqual(chunks[0][0], T - 2 * H)                                         # 從空白的開頭補
        for (a0, a1), (b0, _) in zip(chunks, chunks[1:]):
            self.assertEqual(a1, b0)                                                      # 一段接一段
        self.assertEqual(chunks[-1][1], T)                                                # 補到發現空白那一輪的開頭為止（之後的照常判過了）
        self.assertEqual(len(chunks), 4)                                                  # 2 小時 ÷ 30 分

    def test_second_outage_during_catchup_merges(self):
        state = {"covered_until": (T + H).isoformat(), "gap_from": (T - H).isoformat(), "gap_to": T.isoformat()}
        rest, chunk = D.catchup_plan(state, T + 2 * H, MAX, STEP)                         # 補到一半又停了 1 小時
        self.assertEqual(chunk[0] - chunk[1], T - H)                                      # 先補舊的
        self.assertEqual(rest["gap_to"], (T + 2 * H).isoformat())                         # 新的空白併進來

    def test_too_long_only_recent_max_and_off(self):
        _, chunk = D.catchup_plan({"covered_until": (T - 48 * H).isoformat()}, T, MAX, STEP)
        self.assertEqual(chunk[0] - chunk[1], T - MAX)                                    # 停了兩天：只補最近 6 小時
        self.assertEqual(D.catchup_plan({"covered_until": (T - 2 * H).isoformat()}, T, pd.Timedelta(0), STEP), ({}, None))


class Command(unittest.TestCase):
    def run_detect(self, repo, at=None):
        from common.config import load_config
        cfg = load_config("configs/baseline/series.yaml", "series")
        calls = []
        with mock.patch.object(D, "detect_window", side_effect=lambda r, c, now, w, tz, label="": calls.append((now, w, label)) or (0, [])), \
             mock.patch.object(D, "write_detect_status"), mock.patch.object(D, "now_of", return_value=T):
            D.cmd_detect(cfg, Namespace(window=pd.Timedelta(minutes=10), at=at, config="series.yaml"), repo)
        return calls

    def test_command_fills_one_chunk_and_keeps_progress(self):
        from tests.fakes import MemoryRepos
        repo = MemoryRepos()
        self.assertEqual(len(self.run_detect(repo)), 1)                                   # 第一次：只有照常的
        repo.state.set(D.CATCHUP_KEY + "series.yaml", {"covered_until": (T - H).isoformat()})
        calls = self.run_detect(repo)
        self.assertEqual(len(calls), 2)
        self.assertIn("補算", calls[1][2])
        self.assertEqual((calls[1][0], calls[1][1]), (T - H + STEP, STEP))                # 補 [T-1h, T-30m)；空白到這一輪的開頭 T-10m
        st = repo.state.get(D.CATCHUP_KEY + "series.yaml")
        self.assertEqual((st["gap_from"], st["gap_to"]), ((T - H + STEP).isoformat(), (T - pd.Timedelta(minutes=10)).isoformat()))   # 還剩一段下一輪補
        self.assertEqual(st["covered_until"], T.isoformat())

    def test_replay_with_at_does_not_touch_progress(self):
        from tests.fakes import MemoryRepos
        repo = MemoryRepos()
        repo.state.set(D.CATCHUP_KEY + "series.yaml", {"covered_until": (T - H).isoformat()})
        self.assertEqual(len(self.run_detect(repo, at=T)), 1)
        self.assertEqual(repo.state.get(D.CATCHUP_KEY + "series.yaml"), {"covered_until": (T - H).isoformat()})


if __name__ == "__main__":
    unittest.main()

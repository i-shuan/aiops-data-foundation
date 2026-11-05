"""命令列（typer）跟以前 argparse 的參數一樣：排程表裡的字面指令都還能跑、處理函式拿到的 Namespace 屬性名沒變、
choices 與預設值沒變、錯誤回 2 不丟例外、plugin 的子命令掛在同一個 app。不連 ES：處理函式與 load_config 都用 mock 接住。"""
import contextlib
import glob
import io
import os
import unittest
from unittest import mock

import yaml
from typer.testing import CliRunner

import detector.__main__ as D
import investigator.__main__ as I
from core import plugin
from investigator import cmd_alert
from plugins.factory import cmd_paths

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)

# 子命令 → (處理函式所在的模組, 屬性名, load_config 的 kind)
HANDLERS = {
    "train": (D, "cmd_train", "series"), "detect": (D, "cmd_detect", "series"), "tune": (D, "cmd_tune", "series"),
    "backtest": (D, "cmd_backtest", "series"), "adjust": (D, "cmd_adjust", "series"), "shift": (D, "cmd_shift", "series"),
    "metrics": (D, "cmd_metrics", "series"), "feedback": (D, "cmd_feedback", "series"), "incident": (D, "cmd_incident", "series"),
    "rules": (D, "cmd_rules", "rules"), "peer": (D, "cmd_peer", "peer"), "quality": (D, "cmd_quality", "quality"),
    "trace-collect": (D, "cmd_trace_collect", "trace"), "trace-train": (D, "cmd_trace_train", "trace"),
    "trace-detect": (D, "cmd_trace_detect", "trace"), "trace-merge": (D, "cmd_trace_merge", "trace"),
    "chronic": (D, "cmd_chronic", "chronic"), "prune": (D, "cmd_prune", "prune"), "ingest": (D, "cmd_ingest", "ingest"),
    "paths-merge": (cmd_paths, "cmd_paths_merge", "paths"),
    "alert": (cmd_alert, "run", "rca"),
}


def env(**kw):
    return mock.patch.dict(os.environ, kw)


def run(prog: str, argv: list[str], rc: int = 0):
    """跑一次，回 (處理函式拿到的 args, load_config 收到的 (path, kind))。處理函式回 0。"""
    cmd = next(a for a in argv if not a.startswith("-"))
    mod, name, kind = HANDLERS[cmd]
    seen = {}

    def handler(cfg, args, *rest):
        seen["args"], seen["cfg"] = args, cfg
        return 0

    def load(path, k):
        seen["load"] = (path, k)
        return {}
    main = D.main if prog == "detector" else I.main
    with mock.patch.object(mod, name, handler), mock.patch.object(D, "load_config", load), \
         mock.patch.object(I, "load_config", load), mock.patch.object(cmd_paths, "load_config", load), \
         contextlib.redirect_stdout(io.StringIO()):
        got = main(argv)
    assert got == rc, (argv, got)
    if rc == 0:
        assert seen["load"][1] == kind, (argv, seen["load"])
    return seen.get("args"), seen.get("load")


class ScheduleCommands(unittest.TestCase):
    """四張排程表裡 detector／investigator 的字面指令（scheduler.runner 以子行程 python -m 跑）每一條都還解析得過、到得了處理函式。"""

    def tearDown(self):
        plugin.reset()

    def jobs(self):
        out = []
        for path in sorted(glob.glob(os.path.join(CODE, "configs", "*", "schedule.yaml"))):
            with open(path, encoding="utf-8") as f:
                for j in yaml.safe_load(f)["jobs"]:
                    if j["args"][0] in ("detector", "investigator"):
                        out.append((os.path.relpath(path, CODE), j["args"][0], [str(a) for a in j["args"][1:]]))
        return out

    def test_every_scheduled_command_parses(self):
        jobs = self.jobs()
        self.assertGreaterEqual(len(jobs), 30)
        self.assertTrue(any(a[0] == "paths-merge" for _, _, a in jobs))
        for path, prog, argv in jobs:
            with self.subTest(path=path, argv=argv), env(AIOPS_PLUGIN="factory" if "factory" in path else "observability"):
                args, load = run(prog, argv)
                self.assertEqual(args.config, argv[argv.index("--config") + 1])
                self.assertEqual(load[0], args.config)
                if "--window" in argv:
                    self.assertEqual(args.window, argv[argv.index("--window") + 1])
                if argv[0] in ("rules", "peer", "quality"):
                    self.assertIsNone(args.only)
                self.assertIsNone(getattr(args, "at", None))

    def test_defaults_match_argparse(self):
        self.assertEqual(run("detector", ["detect", "--config", "s.yaml"])[0].window, "30min")
        self.assertEqual(run("detector", ["trace-detect", "--config", "t.yaml"])[0].window, "30min")
        self.assertEqual(run("detector", ["trace-merge", "--config", "t.yaml"])[0].window, "3min")
        self.assertEqual(run("detector", ["ingest", "--config", "i.yaml"])[0].window, "10m")
        with env(AIOPS_PLUGIN="factory"):
            self.assertEqual(run("detector", ["paths-merge", "--config", "p.yaml"])[0].window, "1d")
        a, _ = run("detector", ["train", "--config", "s.yaml"])
        self.assertEqual((a.weeks, a.end), (None, None))
        a, _ = run("detector", ["prune", "--config", "p.yaml"])
        self.assertEqual((a.at, a.dry_run), (None, False))
        a, _ = run("investigator", ["alert", "--config", "r.yaml"])
        self.assertEqual((a.config, a.at), ("r.yaml", None))


class Attributes(unittest.TestCase):
    """處理函式讀的屬性名、型別、choices、預設值跟 argparse 時一樣。"""

    def test_tune_show_and_repeated_match(self):
        a, load = run("detector", ["tune", "--show", "--series", "x", "--match", "service=a", "--match", "service=b"])
        self.assertEqual(a.match, ["service=a", "service=b"])
        self.assertTrue(a.show)
        self.assertEqual((a.list, a.remove), (False, False))
        self.assertEqual((a.series, a.resolution, a.z, a.min_abs, a.confirm_points, a.instant_z), ("x", None, None, None, None, None))
        self.assertEqual(load, ("configs/baseline/series.yaml", "series"))       # --config 的預設
        a, _ = run("detector", ["tune", "--series", "x", "--z", "4", "--min-abs", "0.5", "--confirm-points", "1", "--instant-z", "8", "--list"])
        self.assertEqual((a.z, a.min_abs, a.confirm_points, a.instant_z, a.list, a.match), (4.0, 0.5, 1, 8.0, True, None))

    def test_adjust_positional_action_and_choices(self):
        a, _ = run("detector", ["adjust", "add", "--kind", "exclude_window", "--match", "service=^mes$", "--start", "2026-10-01T00:00:00Z",
                                "--end", "2026-10-01T01:00:00Z", "--reason", "事故"])
        self.assertEqual((a.action, a.kind, a.match), ("add", "exclude_window", ["service=^mes$"]))
        self.assertEqual((a.apply_to, a.status, a.source, a.train, a.mute, a.force, a.id), ("train", "active", "manual", None, False, False, None))
        a, _ = run("detector", ["adjust", "approve", "--id", "p1", "--force", "--by", "amy"])
        self.assertEqual((a.action, a.id, a.force, a.by), ("approve", "p1", True, "amy"))
        a, _ = run("detector", ["adjust", "add", "--kind", "recurring_override", "--cron", "0 20 * * *", "--duration", "4h",
                                "--days-of-month", "-1", "--z", "6", "--mute", "--train", "exclude", "--status", "proposed"])
        self.assertEqual((a.kind, a.cron, a.duration, a.days_of_month, a.z, a.mute, a.train, a.status),
                         ("recurring_override", "0 20 * * *", "4h", "-1", 6.0, True, "exclude", "proposed"))
        for bad in (["adjust", "zzz"], ["adjust", "add", "--kind", "nope"], ["adjust", "add", "--status", "maybe"], ["adjust"]):
            with self.subTest(bad=bad):
                run("detector", bad, rc=2)

    def test_feedback_missed(self):
        a, _ = run("detector", ["feedback", "add", "--verdict", "missed", "--series", "s", "--match", "service=x", "--at", "2026-10-01T00:00:00Z"])
        self.assertEqual((a.action, a.verdict, a.series, a.match, a.at), ("add", "missed", "s", ["service=x"], "2026-10-01T00:00:00Z"))
        self.assertEqual((a.days, a.limit, a.min_false_positive, a.min_recurring), (28, 50, 1, 3))
        self.assertEqual((a.anomaly, a.note, a.by, a.incident, a.alert, a.text, a.root_cause, a.out), (None,) * 8)
        a, _ = run("detector", ["feedback", "export", "--days", "1", "--out", "x.csv", "--min-false-positive", "2"])
        self.assertEqual((a.action, a.days, a.out, a.min_false_positive), ("export", 1, "x.csv", 2))
        run("detector", ["feedback", "nope"], rc=2)

    def test_incident_lists(self):
        a, _ = run("detector", ["incident", "add", "--title", "t", "--start", "s", "--end", "e", "--service", "a", "--service", "b",
                                "--anomaly", "x", "--root-cause", "db", "--action-taken", "重啟", "--exclude-training"])
        self.assertEqual((a.action, a.service, a.anomaly), ("add", ["a", "b"], ["x"]))
        self.assertEqual((a.root_cause, a.action_taken, a.exclude_training, a.id, a.severity), ("db", "重啟", True, None, None))
        a, _ = run("detector", ["incident", "resolve", "--id", "i1"])
        self.assertEqual((a.action, a.id, a.service, a.anomaly), ("resolve", "i1", None, None))

    def test_metrics_from_to_and_backtest_ids(self):
        a, _ = run("detector", ["metrics", "--from", "2026-10-01", "--to", "2026-10-08", "--compare-from", "2026-09-01", "--compare-to", "2026-09-08"])
        self.assertEqual((a.start, a.end, a.cstart, a.cend, a.out), ("2026-10-01", "2026-10-08", "2026-09-01", "2026-09-08", None))
        run("detector", ["metrics", "--from", "2026-10-01"], rc=2)                # --to 必填
        a, _ = run("detector", ["backtest", "--id", "a", "--id", "b", "--weeks", "2", "--verbose"])
        self.assertEqual((a.id, a.weeks, a.verbose, a.group, a.at), (["a", "b"], 2, True, False, None))
        run("detector", ["backtest"], rc=2)                                        # --id 必填

    def test_required_config(self):
        for argv in (["detect"], ["rules"], ["train", "--weeks", "4"], ["trace-merge"]):
            with self.subTest(argv=argv):
                run("detector", argv, rc=2)
        run("investigator", ["alert"], rc=2)

    def test_investigator_attributes(self):
        seen = {}
        with mock.patch.object(I, "investigate_full", lambda cfg, args: (seen.setdefault("args", args) and "報告", {})), \
             mock.patch.object(I, "load_config", lambda p, k: {"kind": k}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(I.main(["investigate", "--config", "r.yaml", "--service", "mes-api", "--route", "GET /lots/{id}", "--window", "1h"]), 0)
        a = seen["args"]
        self.assertEqual((a.service, a.route, a.window, a.start, a.end, a.at, a.out), ("mes-api", "GET /lots/{id}", "1h", None, None, None, None))
        with mock.patch.object(I, "topology", lambda cfg, args: seen.setdefault("topo", (cfg, args)) and "拓撲"), \
             mock.patch.object(I, "load_config", lambda p, k: {"kind": k}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(I.main(["topology", "--config", "r.yaml"]), 0)
        cfg, a = seen["topo"]
        self.assertEqual((cfg, a.window, a.sample, a.at), ({"kind": "rca"}, "1h", 2000, None))


class Errors(unittest.TestCase):
    def tearDown(self):
        plugin.reset()

    def test_bogus_returns_2_without_raising(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(D.main(["bogus"]), 2)
            self.assertEqual(D.main([]), 2)
            self.assertEqual(I.main(["bogus"]), 2)

    def test_help_lists_all_commands_and_plugin_ones(self):
        with env(AIOPS_PLUGIN="observability"):
            r = CliRunner().invoke(D.build_app(), ["--help"])
        self.assertEqual(r.exit_code, 0)
        for name in D.COMMANDS:
            self.assertIn(name, r.output)
        self.assertNotIn("paths-merge", r.output)
        with env(AIOPS_PLUGIN="factory"):
            plugin.reset()
            r = CliRunner().invoke(D.build_app(), ["--help"])
            self.assertIn("paths-merge", r.output)
            r = CliRunner().invoke(D.build_app(), ["paths-merge", "--help"])
            self.assertEqual(r.exit_code, 0)
            for opt in ("--config", "--window", "--at"):
                self.assertIn(opt, r.output)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(D.main(["--help"]), 0)
            self.assertEqual(I.main(["--help"]), 0)
        r = CliRunner().invoke(I.app, ["--help"])
        for name in ("investigate", "topology", "alert", "similar-eval", "import-incidents"):
            self.assertIn(name, r.output)

    def test_import_incidents_without_file_returns_2(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(I.main(["import-incidents", "--dry-run"]), 2)
        self.assertIn("要給檔案", out.getvalue())


if __name__ == "__main__":
    unittest.main()

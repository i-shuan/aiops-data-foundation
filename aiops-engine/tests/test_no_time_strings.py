"""時間字串只在邊界解析：src/ 裡除了 common/clock，沒有人自己寫 fromisoformat／strptime 之外的時間長度解析，
也沒有第二個 `_dur`、`to_freq`、`_now`。命令列在 interface/cli 用 typer 的 parser，設定檔用 pydantic（Dur、Tz）。"""
import ast
import pathlib
import unittest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
DUPLICATE_NAMES = {"_dur", "to_freq", "_freq", "_now", "duration", "now_ts", "parse_ts"}


class OneClock(unittest.TestCase):
    def test_no_second_definition_of_time_helpers(self):
        bad = []
        for p in SRC.rglob("*.py"):
            if "common/clock" in p.as_posix():
                continue
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(n, ast.FunctionDef) and n.name in DUPLICATE_NAMES:
                    bad.append(f"{p.relative_to(SRC)}:{n.lineno} def {n.name}")
        self.assertEqual(bad, [], "時間長度、頻率、「現在」只在 common/clock 定義一份")

    def test_services_do_not_parse_cli_strings(self):
        """cmd_*(config, args) 收到的 args.at／window 已經是 Timestamp／Timedelta：下面這些寫法代表又在服務裡解析字串了。"""
        bad = []
        for p in SRC.rglob("*.py"):
            rel = p.relative_to(SRC).as_posix()
            if rel.startswith(("interface/", "common/clock")) or rel == "learn/feedback_service.py":   # feedback add 的 --at 是人填的「漏報發生時間」字串（連同時區一起處理），不是「現在」
                continue
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Attribute) and isinstance(n.args[0].value, ast.Name) \
                        and n.args[0].value.id == "args" and n.args[0].attr in ("at", "window"):
                    f = ast.unparse(n.func)
                    if f in ("pd.Timestamp", "pd.Timedelta", "pandas.Timestamp", "datetime.fromisoformat"):
                        bad.append(f"{rel}:{n.lineno} {ast.unparse(n)}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()

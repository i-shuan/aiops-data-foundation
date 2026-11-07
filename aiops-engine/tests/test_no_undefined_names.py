"""沒有用到卻沒 import 的名字（pyflakes 的 undefined name）。

搬檔、改名、拆檔時最容易掉的就是 import；函式裡的 NameError 要執行到那一行才炸，單元測試沒跑到的指令（例如 trace-train、
trace-detect）就一直壞著。這個測試靜態掃全部程式。沒裝 pyflakes 就跳過（開發用，不在 requirements.txt）。"""
import pathlib
import unittest

try:
    from pyflakes import api, reporter
except ImportError:          # pragma: no cover
    api = None

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


@unittest.skipIf(api is None, "沒裝 pyflakes")
class NoUndefinedNames(unittest.TestCase):
    def test_src_and_main(self):
        import io
        out = io.StringIO()
        rep = reporter.Reporter(out, out)
        for p in sorted(SRC.rglob("*.py")) + [SRC.parent / "main.py"]:
            api.check(p.read_text(encoding="utf-8"), str(p.relative_to(SRC.parent)), rep)
        bad = [l for l in out.getvalue().splitlines() if "undefined name" in l]
        self.assertEqual(bad, [], "用到卻沒 import 的名字：\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()

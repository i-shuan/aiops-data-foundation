"""設定是 Pydantic 模型，參數叫 `<種類>_config`（series_config、rca_config…）：
src/ 裡不准有叫 cfg 的參數或變數，也不准用 dict 的寫法讀設定模型（.to_dict()、**config）。環境變數的 Settings 叫 env。"""
import ast
import pathlib
import unittest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


class NoCfgNames(unittest.TestCase):
    def test_no_cfg_identifier(self):
        bad = []
        for p in SRC.rglob("*.py"):
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(n, ast.arg) and n.arg == "cfg":
                    bad.append(f"{p.relative_to(SRC)}:{n.lineno} 參數 cfg")
                elif isinstance(n, ast.Name) and n.id == "cfg" and isinstance(n.ctx, ast.Store):
                    bad.append(f"{p.relative_to(SRC)}:{n.lineno} 變數 cfg")
        self.assertEqual(bad, [], "設定參數叫 <種類>_config，環境變數的 Settings 叫 env")

    def test_no_dict_style_reading_of_models(self):
        bad = []
        for p in SRC.rglob("*.py"):
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Attribute) and n.attr == "to_dict" and isinstance(n.value, ast.Attribute) and n.value.attr in ("train", "detect", "shift", "severity"):
                    bad.append(f"{p.relative_to(SRC)}:{n.lineno} .{n.value.attr}.to_dict()（改 model_dump）")
        self.assertEqual(bad, [])

    def test_config_params_are_not_annotated_as_dict(self):
        bad = []
        for p in SRC.rglob("*.py"):
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for a in n.args.args + n.args.kwonlyargs:
                        if a.arg.endswith("_config") and a.annotation is not None and "dict" in ast.unparse(a.annotation).lower():
                            bad.append(f"{p.relative_to(SRC)}:{n.lineno} {a.arg}: {ast.unparse(a.annotation)}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()

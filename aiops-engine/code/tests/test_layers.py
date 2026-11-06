"""依賴方向（docs/pdca/meta/2026-11-clean-code.md C）：core → sources → detector、investigator → feedback → 最上層（api、scheduler、tasks、plugins、models）。
誰 import 誰只能往右；__main__.py 是入口（組合根）不算。函式內的 import 只允許名單上那幾個（真的要 lazy 的）。
加了反向 import 這裡會紅——讀到這裡的人：先想東西該不該搬，不要改名單。"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = {p.name for p in ROOT.iterdir() if p.is_dir() and (p / "__init__.py").exists()} - {"tests"}
ALLOWED = {                       # 套件 → 它可以 import 的套件（自己以外）
    "core": set(),
    "sources": {"core"},
    "detector": {"core", "sources"},
    "investigator": {"core", "sources"},
    "feedback": {"core", "sources", "detector", "investigator"},
}
# 函式內才 import 的（其他都要在檔頭）：sqlalchemy 是選配、tasks 要 broker、sources 的工廠避免套件內循環
LAZY_OK = {("feedback/importer.py", "sqlalchemy"), ("scheduler/__main__.py", "tasks"), ("sources/base.py", ".es_source")}


def modules():
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if rel.parts[0] in PACKAGES and "__pycache__" not in rel.parts:
            yield rel, ast.parse(p.read_text(encoding="utf-8"))


def imported_packages(tree) -> set[str]:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            out.add(n.module.split(".")[0])
        elif isinstance(n, ast.Import):
            out.update(a.name.split(".")[0] for a in n.names)
    return out & PACKAGES


class Layers(unittest.TestCase):
    def test_direction(self):
        bad = []
        for rel, tree in modules():
            pkg = rel.parts[0]
            if pkg not in ALLOWED or rel.name == "__main__.py":
                continue
            extra = imported_packages(tree) - ALLOWED[pkg] - {pkg}
            if extra:
                bad.append(f"{rel} → {sorted(extra)}")
        self.assertEqual(bad, [], "反向依賴：\n" + "\n".join(bad))

    def test_no_inline_imports(self):
        bad = []
        for rel, tree in modules():
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for sub in ast.walk(n):
                        if isinstance(sub, (ast.Import, ast.ImportFrom)):
                            name = ("." * sub.level + (sub.module or "")) if isinstance(sub, ast.ImportFrom) else sub.names[0].name
                            if (str(rel), name) not in LAZY_OK:
                                bad.append(f"{rel}:{sub.lineno} {name}")
        self.assertEqual(bad, [], "函式內 import（該放檔頭，或加進 LAZY_OK 並說明理由）：\n" + "\n".join(bad))

    def test_contracts_live_in_core(self):
        """兩個階段以上都碰的 ES 索引，index 名與 mapping 只在 core/ 定義一次。"""
        for name in ("aiops-anomalies", "aiops-alerts", "aiops-incidents", "aiops-tuning"):
            where = [str(rel) for rel, tree in modules() for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == name]
            self.assertEqual([w for w in where if not w.startswith("core/")], [], f"{name} 的字面值只能在 core/：{where}")


if __name__ == "__main__":
    unittest.main()

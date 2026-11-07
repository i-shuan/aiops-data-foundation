"""依賴方向（docs/pdca/meta/2026-11-feature-folders.md）：interface → learn → incident → llm → detect → clean → common。
誰 import 誰只能往右（common 最底，什麼功能都不依賴）；main.py（入口）不算。
函式內的 import 只允許名單上那幾個。索引名只能在各功能資料夾的 store/ 與 common/。
common/infra/repos.py 用名稱表在第一次取用才載入各功能的 repository（importlib），所以不算靜態依賴。
`if TYPE_CHECKING:` 底下的 import 只給型別檢查看、執行時不載入，也不算（common/config/loader.py 的 overload、scenario/base.py 用）。
加了反向 import 這裡會紅——讀到這裡的人：先想東西該不該搬，不要改名單。"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "src"
LAYERS = ("common", "clean", "detect", "llm", "incident", "learn", "interface")       # 由下往上
ALLOWED = {name: set(LAYERS[:i]) for i, name in enumerate(LAYERS)}
# 函式內才 import 的（其他都要在檔頭）：sqlalchemy 是選配、worker 要 broker
LAZY_OK = {("learn/history_importer.py", "sqlalchemy"), ("interface/jobs/local.py", "interface.jobs.worker")}
# 索引名的字面值只能在各功能的 store/（資料物件定義、repository 讀寫）與 common/
INDEX_LITERALS = ("aiops-anomalies", "aiops-alerts", "aiops-incidents", "aiops-feedback", "aiops-tuning", "aiops-baseline",
                  "aiops-trace-segments", "aiops-state", "aiops-chronic")
# 還沒清掉的反向依賴：S2 之後是空的，加了反向 import 這裡會紅
KNOWN: set[str] = set()


def modules():
    for layer in LAYERS:
        for p in (PKG / layer).rglob("*.py"):
            if "__pycache__" not in p.parts:
                yield p.relative_to(PKG), ast.parse(p.read_text(encoding="utf-8"))


def _runtime_nodes(tree):
    """ast.walk，但跳過 `if TYPE_CHECKING:` 的內容。"""
    stack = [tree]
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "TYPE_CHECKING":
            stack.extend(n.orelse)
            continue
        stack.extend(ast.iter_child_nodes(n))


def imported_layers(tree) -> set[str]:
    out = set()
    for n in _runtime_nodes(tree):
        mod = None
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mod = n.module
        elif isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.name.split(".")[0])
        if mod:
            out.add(mod.split(".")[0])
    return out & set(LAYERS)


class Layers(unittest.TestCase):
    def test_scans_something(self):
        self.assertGreater(len(list(modules())), 60, "沒掃到檔案：LAYERS 的資料夾名跟實際對不上")

    def test_direction(self):
        bad = []
        for rel, tree in modules():
            layer = rel.parts[0]
            if layer not in ALLOWED:
                continue
            extra = imported_layers(tree) - ALLOWED[layer] - {layer}
            if extra:
                bad.append(f"{rel} → {sorted(extra)}")
        self.assertEqual(sorted(set(bad) - KNOWN), [], "反向依賴：\n" + "\n".join(bad))
        self.assertEqual(sorted(KNOWN - set(bad)), [], "KNOWN 裡已經清掉的要拿掉")

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

    def test_index_literals_only_in_store(self):
        for name in INDEX_LITERALS:
            where = [str(rel) for rel, tree in modules() for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == name]
            outside = [w for w in where if not (w.startswith("common/") or "/store/" in w)]
            self.assertEqual(outside, [], f"{name} 的字面值只能在 */store/ 與 common/：{where}")


if __name__ == "__main__":
    unittest.main()

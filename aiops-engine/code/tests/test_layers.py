"""依賴方向（docs/pdca/meta/2026-11-target-architecture.md）：interface → service → scenario → core；config 是葉節點。
誰 import 誰只能往右；相容入口（detector/、investigator/、scheduler/、tasks/、api/ 的 __main__）不算。
函式內的 import 只允許名單上那幾個。索引名與 ES 查詢語法只能在 core/repository/（S1 一個實體一個打開）。
加了反向 import 這裡會紅——讀到這裡的人：先想東西該不該搬，不要改名單。"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "aiops"
LAYERS = ("config", "core", "scenario", "service", "interface")       # 由下往上
ALLOWED = {"config": set(), "core": {"config"}, "scenario": {"config", "core"},
           "service": {"config", "core", "scenario"}, "interface": {"config", "core", "scenario", "service"}}
# 函式內才 import 的（其他都要在檔頭）：sqlalchemy 是選配、worker 要 broker
LAZY_OK = {("service/feedback/importer.py", "sqlalchemy"), ("interface/jobs/local.py", "aiops.interface.jobs.worker"),
           ("core/repository/base.py", "aiops.core.repository.series")}      # S1 拆掉 base 後這條會消失
# 索引名的字面值只能在 core/repository/（S1 每做完一個實體就把它加進來）
INDEX_LITERALS = ()
# 還沒清掉的反向依賴（S0 搬完的現況；S1 拆 repository、S2 做 Scenario 時逐條刪，刪光這個集合就空了）
KNOWN = {"core/repository/owner.py → ['scenario']", "core/repository/rca_queries.py → ['scenario']", "core/entity/trace.py → ['scenario']",
         "scenario/it/chain.py → ['service']", "scenario/it/__init__.py → ['service']", "scenario/factory/paths.py → ['service']",
         "scenario/factory/__init__.py → ['service']", "scenario/factory/commands.py → ['service']", "scenario/factory/ingest_job.py → ['service']"}


def modules():
    for p in PKG.rglob("*.py"):
        if "__pycache__" not in p.parts:
            yield p.relative_to(PKG), ast.parse(p.read_text(encoding="utf-8"))


def imported_layers(tree) -> set[str]:
    out = set()
    for n in ast.walk(tree):
        mod = None
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mod = n.module
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("aiops."):
                    out.add(a.name.split(".")[1])
        if mod and mod.startswith("aiops.") and mod != "aiops":
            out.add(mod.split(".")[1])
    return out & set(LAYERS)


class Layers(unittest.TestCase):
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

    def test_index_literals_only_in_repository(self):
        for name in INDEX_LITERALS:
            where = [str(rel) for rel, tree in modules() for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == name]
            self.assertEqual([w for w in where if not w.startswith("core/repository/")], [], f"{name} 的字面值只能在 core/repository/：{where}")


if __name__ == "__main__":
    unittest.main()

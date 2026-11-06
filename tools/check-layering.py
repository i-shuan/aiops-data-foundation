#!/usr/bin/env python3
"""量 aiops-engine/code 的分層與內聚（docs/pdca/meta/2026-11-layering.md 的驗收指標）。

  py tools/check-layering.py              印全部指標，違規的回 1
  py tools/check-layering.py --report     只印、不判定（看進度）
  py tools/check-layering.py --only cycles,imports,names,words   只判定這幾項

指標：
  layers   套件只能往下 import（含函式內）；雙向相依數要是 0
  cycles   模組層循環數要是 0
  imports  函式內 import 內部套件：只允許 ALLOWED_INNER 列的（core.plugin 的 importlib、入口層）
  names    非測試 .py 不能同名（plugin 底下的 chain／changes 例外：每個情境各一份是設計）
  words    core/、detector/、investigator/ 的「識別字」不能有情境詞（IT：service、route、span…；工廠：equipment、lot…）。
           只數程式結構（函式、類別、變數、參數、屬性的名字，拆 snake_case 比對）；字串與註解不算——
           ES 欄位名（al["service"]）、CLI 範例、說明文字是對外契約或文件，不是程式認得情境。
           另外印出「字串與註解裡」的次數當參考（不判定）。sources/traces.py 是 trace 格式解析，例外
"""
from __future__ import annotations

import argparse
import ast
import collections
import re
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1] / "aiops-engine" / "code"
PKGS = ("core", "sources", "detector", "investigator", "plugins", "api", "scheduler", "tasks")
# 只能往下：key 可以 import value 裡的套件
ALLOWED = {
    "core": set(),
    "sources": {"core"},
    "detector": {"core", "sources"},
    "investigator": {"core", "sources", "detector"},
    "plugins": {"core", "sources", "detector", "investigator"},
    "api": {"core", "sources", "detector", "investigator", "plugins", "scheduler", "tasks"},
    "scheduler": {"core"},                       # runner 不碰 tasks；--enqueue 由 __main__ 注入
    "tasks": {"core", "scheduler"},
}
MODULE_EXCEPTIONS = {("scheduler.__main__", "tasks")}          # 入口可以拿佇列
ALLOWED_INNER = {"core.plugin", "detector.__main__", "investigator.__main__", "api.__main__", "api.server", "scheduler.__main__", "tasks"}
IT_WORDS = r"\b(service|route|span|trace|pod|k8s|kubernetes|http)\b"
FACTORY_WORDS = r"\b(equipment|recipe|fdc|lot|mes|s6f11|oos|wafer)\b"
WORD_DIRS = ("core", "detector", "investigator")
WORD_EXCEPTIONS = {"sources/traces.py"}
# 整個識別字跳過：這兩個等於 YAML key（status.confirm_span、alerts.severity.min_span_critical），是對外契約，
# 「span」是時間跨度（連續幾個點、連續多久），不是 OTel 的 span
IDENT_EXCEPTIONS = {"confirm_span", "min_span_critical"}
NAME_EXCEPTIONS = {"chain.py", "changes.py", "__init__.py", "__main__.py", "config.py", "command.py"}


def modname(p: Path) -> str:
    parts = list(p.relative_to(CODE).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def files():
    return [p for p in CODE.rglob("*.py") if ".venv" not in p.parts and "__pycache__" not in p.parts and p.relative_to(CODE).parts[0] != "tests"]


def imports():
    """[(模組, 被 import 的內部模組或套件, 是否在函式內)]"""
    out = []
    for p in files():
        m = modname(p)
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            inner = node.col_offset > 0
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    base = m.split(".")
                    base = base[:len(base) - node.level + (1 if p.name == "__init__.py" else 0)]
                    tgt = ".".join(base) + (f".{node.module}" if node.module else "")
                else:
                    tgt = node.module or ""
                names = [tgt]
            else:
                names = [a.name for a in node.names]
            for n in names:
                if n.split(".")[0] in PKGS:
                    out.append((m, n, inner))
    return out


def check_layers(imps):
    bad = []
    for m, n, _ in imps:
        a, b = m.split(".")[0], n.split(".")[0]
        if a == b or (m, b) in MODULE_EXCEPTIONS:
            continue
        if b not in ALLOWED.get(a, set()):
            bad.append(f"{m} → {n}")
    return sorted(set(bad))


def check_cycles(imps):
    mods = {modname(p) for p in files()}

    def resolve(n):
        while n and n not in mods:
            n = n.rsplit(".", 1)[0] if "." in n else ""
        return n
    g = collections.defaultdict(set)
    for m, n, _ in imps:
        r = resolve(n)
        if r and r != m:
            g[m].add(r)
    color, found = {}, []

    def dfs(u, stack):
        color[u] = 1
        stack.append(u)
        for v in g.get(u, ()):
            if color.get(v, 0) == 0:
                dfs(v, stack)
            elif color.get(v) == 1:
                found.append(" → ".join(stack[stack.index(v):] + [v]))
        stack.pop()
        color[u] = 2
    sys.setrecursionlimit(10000)
    for m in sorted(g):
        if color.get(m, 0) == 0:
            dfs(m, [])
    return found


def check_inner(imps):
    return sorted({f"{m}: {n}" for m, n, inner in imps if inner and m not in ALLOWED_INNER and m.split(".")[0] not in ALLOWED_INNER})


def check_names():
    seen = collections.defaultdict(list)
    for p in files():
        if p.name not in NAME_EXCEPTIONS:
            seen[p.name].append(str(p.relative_to(CODE)).replace("\\", "/"))
    return [f"{k}: {', '.join(v)}" for k, v in sorted(seen.items()) if len(v) > 1]


def _identifiers(tree) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node.name)
        elif isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, ast.arg):
            out.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            out.append(node.arg)
    return [n for n in out if n not in IDENT_EXCEPTIONS]


def check_words():
    """回傳 (違規清單, 參考清單)：違規是識別字裡的情境詞；參考是字串與註解裡的（只印不判）。"""
    bad, info = [], []
    for p in files():
        rel = str(p.relative_to(CODE)).replace("\\", "/")
        if rel.split("/")[0] not in WORD_DIRS or rel in WORD_EXCEPTIONS:
            continue
        text = p.read_text(encoding="utf-8")
        tree = ast.parse(text)
        idents = " ".join(w for name in _identifiers(tree) for w in name.split("_"))
        it_id = len(re.findall(IT_WORDS, idents, re.I))
        fa_id = len(re.findall(FACTORY_WORDS, idents, re.I))
        if it_id or fa_id:
            bad.append(f"{rel}: 識別字 IT 詞 {it_id}、工廠詞 {fa_id}")
        it_all = len(re.findall(IT_WORDS, text, re.I))
        fa_all = len(re.findall(FACTORY_WORDS, text, re.I))
        if (it_all - it_id) or (fa_all - fa_id):
            info.append(f"{rel}: 字串／註解 IT 詞 {it_all - it_id}、工廠詞 {fa_all - fa_id}")
    return bad, info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--only", default="layers,cycles,imports,names,words")
    a = ap.parse_args()
    imps = imports()
    words_bad, words_info = check_words()
    results = {"layers": check_layers(imps), "cycles": check_cycles(imps), "imports": check_inner(imps),
               "names": check_names(), "words": words_bad}
    labels = {"layers": "往上 import（違反分層）", "cycles": "模組層循環", "imports": "函式內 import 內部套件（白名單外）",
              "names": "同名 .py", "words": "core／detector／investigator 的情境詞"}
    rc = 0
    for k, v in results.items():
        mark = "✓" if not v else "✗"
        print(f"{mark} {labels[k]}：{len(v)}")
        for line in v[:40]:
            print(f"    {line}")
        if v and not a.report and k in a.only.split(","):
            rc = 1
    print(f"ℹ 字串／註解裡的情境詞（參考，不判定）：{len(words_info)} 檔")
    for line in words_info[:40]:
        print(f"    {line}")
    return rc


if __name__ == "__main__":
    sys.exit(main())

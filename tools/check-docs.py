#!/usr/bin/env python3
"""文件檢查（docs/pdca/meta/2026-10-docs-governance.md）：

  python3 tools/check-docs.py          檢查：連結沒斷、每份 md 都在索引裡、活的文件沒有舊說法；有問題 exit 1
  python3 tools/check-docs.py --toc    重新產生 <!-- toc --> 和 <!-- /toc --> 中間的目錄（手寫的目錄一定會腐化）

只用標準函式庫。在 repo 根目錄跑（或任何地方，會自己找 git 根目錄）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
INDEX = ROOT / "docs/README.md"
PDCA_INDEX = ROOT / "docs/pdca/README.md"

# 舊說法 → 正式術語（唯一來源是 CLAUDE.md 的術語對照表；這裡只放「一定不會是別的意思」的片語）。
# 「一把」是量詞（一把 key）、「一件」「老師傅」「值班」是產品用語，不在這裡。
FORBIDDEN = {"跟兄弟比": "Pod 同儕橫向比對", "保底規則": "靜態臨界值規則", "一把寫入": "集中式資料寫入憑證"}
# 這些是紀錄或對照表本身，不檢查舊說法：PDCA 紀錄（當時的措辭）、術語表、這個腳本
TERM_EXEMPT = ("docs/pdca/phase", "docs/pdca/meta/", "docs/pdca/2026-10-docs-governance.md", "docs/pdca/TEMPLATE.md", "CLAUDE.md", "tools/check-docs.py")

LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")


def md_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    return [ROOT / f for f in out]


def lines_outside_code(text: str):
    """(行號, 行)，跳過程式碼區塊。"""
    fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fence = not fence
            continue
        if not fence:
            yield n, line


def slug(heading: str) -> str:
    """GitHub 的標題錨點：小寫、去掉標點（保留字母數字、中文、底線、減號）、空白變減號。"""
    h = re.sub(r"[`*~]|<[^>]+>|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "" if m.group(0).startswith("[") else "", heading.strip().lower())
    h = re.sub(r"[^\w\- 　]", "", h, flags=re.UNICODE)
    return re.sub(r"[ 　]", "-", h)


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    for _, line in lines_outside_code(path.read_text(encoding="utf-8")):
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            s = slug(m.group(1))
            k = seen.get(s, 0)
            seen[s] = k + 1
            out.add(s if k == 0 else f"{s}-{k}")
    return out


def check_links(files: list[Path]) -> list[str]:
    bad, cache = [], {}
    for f in files:
        for n, line in lines_outside_code(f.read_text(encoding="utf-8")):
            line = re.sub(r"`[^`]*`", "", line)    # 行內程式碼裡的 [x](y) 不是連結
            for m in LINK.finditer(line):
                target = m.group(1)
                if re.match(r"^[a-z]+:", target) or target.startswith("#") and False:
                    continue
                path, _, frag = target.partition("#")
                dest = f if not path else (f.parent / path).resolve()
                if not dest.exists():
                    bad.append(f"{f.relative_to(ROOT)}:{n} 連結指到不存在的 {target}")
                    continue
                if frag and dest.suffix == ".md":
                    if dest not in cache:
                        cache[dest] = anchors(dest)
                    if frag.lower() not in cache[dest] and frag not in cache[dest]:
                        bad.append(f"{f.relative_to(ROOT)}:{n} 找不到章節 {target}")
    return bad


def linked_from(index: Path) -> set[Path]:
    got = set()
    for _, line in lines_outside_code(index.read_text(encoding="utf-8")):
        for m in LINK.finditer(line):
            p = m.group(1).partition("#")[0]
            if p and not re.match(r"^[a-z]+:", p):
                got.add((index.parent / p).resolve())
    return got


def check_index(files: list[Path]) -> list[str]:
    bad = []
    if not INDEX.exists():
        return ["docs/README.md 不存在"]
    top, pdca = linked_from(INDEX), linked_from(PDCA_INDEX)
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        if f in (INDEX, PDCA_INDEX):
            continue
        if rel.startswith("docs/pdca/"):    # PDCA 紀錄在 docs/pdca/README.md 裡有一份索引；docs/README.md 只要指到它
            if f.resolve() not in pdca and rel != "docs/pdca/TEMPLATE.md":
                bad.append(f"{rel} 不在 docs/pdca/README.md 的索引裡")
        elif f.resolve() not in top:
            bad.append(f"{rel} 不在 docs/README.md 的索引裡")
    if PDCA_INDEX.resolve() not in top:
        bad.append("docs/README.md 沒有連到 docs/pdca/README.md")
    return bad


def check_terms(files: list[Path]) -> list[str]:
    bad = []
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        if rel.startswith(TERM_EXEMPT) or rel in TERM_EXEMPT:
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for old, new in FORBIDDEN.items():
                if old in line:
                    bad.append(f"{rel}:{n} 舊說法「{old}」，正式術語是「{new}」")
    return bad


def toc_block(text: str) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for _, line in lines_outside_code(text):
        m = re.match(r"^(#{2,3})\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        s = slug(m.group(2))
        k = seen.get(s, 0)
        seen[s] = k + 1
        out.append(f"{'  ' * (len(m.group(1)) - 2)}- [{m.group(2)}](#{s if k == 0 else f'{s}-{k}'})")
    return out


def update_toc(files: list[Path]) -> int:
    n = 0
    for f in files:
        text = f.read_text(encoding="utf-8")
        m = re.search(r"(<!-- toc -->\n).*?(<!-- /toc -->)", text, re.S)
        if not m:
            continue
        body = "\n".join(toc_block(text[:m.start()] + text[m.end():])) + "\n"    # 目錄自己不算
        new = text[:m.end(1)] + body + text[m.start(2):]
        if new != text:
            f.write_text(new, encoding="utf-8")
            print(f"更新目錄：{f.relative_to(ROOT)}")
            n += 1
    return n


def main() -> int:
    files = md_files()
    if "--toc" in sys.argv:
        update_toc(files)
        return 0
    problems = {"連結": check_links(files), "索引": check_index(files), "舊說法": check_terms(files)}
    for kind, items in problems.items():
        print(f"{'✗' if items else '✓'} {kind}：{len(items)} 個問題")
        for i in items[:40]:
            print("   ", i)
        if len(items) > 40:
            print(f"    …還有 {len(items) - 40} 個")
    return 1 if any(problems.values()) else 0


if __name__ == "__main__":
    sys.exit(main())

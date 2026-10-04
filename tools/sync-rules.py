#!/usr/bin/env python
"""紅線的正本是 AGENTS.md。這支確認另外兩個 AI 工具讀到的是同一份。

    python tools/sync-rules.py          檢查(不一致就回 1)
    python tools/sync-rules.py --fix    從正本複製到 copilot-instructions.md

三個工具讀三個不同的檔名:

    AGENTS.md                        正本。Copilot coding agent / Cursor 直接讀
    CLAUDE.md                        Claude Code 讀。一行 @AGENTS.md 匯入,沒有副本
    .github/copilot-instructions.md  Copilot Chat / inline 讀。只能複製

為什麼不乾脆刪掉 CLAUDE.md:Claude Code v2.1.277 以後會自己讀 AGENTS.md,
但升級後的第一個 session、關掉內建 agents-md plugin 時讀不到。留著匯入在
任何情況都讀得到,官方文件也寫明不會因此讀兩次。

Copilot 不支援匯入(inline completion 根本沒有工具可以讀檔),這台機器又
`core.symlinks = false`,git 會把 symlink 存成純文字檔 —— 所以那一份只能複製。

⚠️ 這個 repo 的規則是「同一件事不開兩份」,因為會漂。沒得選的那一份改成
   **有檢查的重複**:`--fix` 複製,不帶參數驗。漂掉會當場紅字。

⚠️ CLAUDE.md 少了 `@AGENTS.md` 那一行,Claude Code 就一條紅線都讀不到,
   **而且不會報錯** —— 它只是少載入一個檔。所以這支也守那一行。
"""

from __future__ import annotations

import io
import os
import re
import sys

# ⚠️ Windows 的中文 console 預設 cp950,印 ✅ 會 UnicodeEncodeError。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = "AGENTS.md"
IMPORTER = "CLAUDE.md"
COPIES = [".github/copilot-instructions.md"]

BLOCK = re.compile(
    r"(<!-- RULES:START -->)(.*?)(<!-- RULES:END -->)", re.S
)


def read(rel: str) -> str:
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def extract(rel: str) -> str | None:
    m = BLOCK.search(read(rel))
    return m.group(2) if m else None


def check_importer() -> int:
    """CLAUDE.md 要有獨立一行 @AGENTS.md,而且自己不能再放一份紅線。"""
    text = read(IMPORTER)
    bad = 0
    if "@" + SOURCE not in [l.strip() for l in text.splitlines()]:
        print(f"❌ {IMPORTER} 沒有獨立一行的 @{SOURCE} —— "
              f"Claude Code 會讀不到紅線,而且不報錯")
        bad += 1
    if BLOCK.search(text):
        print(f"❌ {IMPORTER} 裡又出現 RULES 區塊 —— 正本是 {SOURCE},兩份會漂")
        bad += 1
    if not bad:
        print(f"  OK  {IMPORTER}  用 @{SOURCE} 匯入")
    return bad


def main() -> int:
    fix = "--fix" in sys.argv
    src = extract(SOURCE)
    if src is None:
        print(f"❌ {SOURCE} 裡找不到 <!-- RULES:START --> 區塊")
        return 1

    bad = check_importer()
    for rel in COPIES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f"❌ {rel} 不存在")
            bad += 1
            continue
        cur = extract(rel)
        if cur is None:
            print(f"❌ {rel} 裡找不到 <!-- RULES:START --> 區塊")
            bad += 1
            continue
        if cur == src:
            print(f"  OK  {rel}")
            continue
        if fix:
            text = read(rel)
            text = BLOCK.sub(lambda m: m.group(1) + src + m.group(3), text, count=1)
            io.open(path, "w", encoding="utf-8", newline="\n").write(text)
            print(f"  FIX {rel}  已從 {SOURCE} 同步")
        else:
            print(f"❌ {rel} 的紅線跟 {SOURCE} 不一樣 —— 跑 "
                  f"`python tools/sync-rules.py --fix`")
            bad += 1

    if bad:
        print(f"\n❌ {bad} 處對不上")
        return 1
    print(f"\n✅ 紅線一致({SOURCE} 是正本,{IMPORTER} 匯入,其餘複製)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

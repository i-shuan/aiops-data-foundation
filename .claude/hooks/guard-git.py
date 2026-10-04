# -*- coding: utf-8 -*-
"""Claude Code 的 PreToolUse hook：擋掉會弄丟東西的 git 指令。

CLAUDE.md 的紅線「不用破壞性 git」是文字 —— AI 讀過照樣會跑。這個 repo
實際發生過：紅線已經寫著不要 `git checkout <file>`，AI 還是跑了，把使用者
81 行還沒 commit 的 README 修改洗掉，沒有任何錯誤訊息。

這支是確定性的那一層：AI 聽不聽話無所謂，指令到這裡就停。

  - reset --hard：未提交的修改直接消失
  - checkout / restore 有修改的檔：同上，範圍小一點。踩過的就是這個
  - checkout -f、switch --discard-changes：切分支順便丟掉修改
  - clean -f（不帶 -n）：未追蹤的檔直接刪掉，git 裡沒有備份
  - push --force / -f / +ref / --force-with-lease：改寫遠端歷史

⚠️ 只對 Claude Code 有效。Copilot / Cursor 不讀 .claude/settings.json。
⚠️ 擋的是「順手跑了」，不是「故意繞過」。`bash -c "..."` 包一層就看不到。

協定：Claude Code 把工具呼叫用 JSON 從 stdin 丟進來。exit 2 = 擋下，
stderr 回給 AI；exit 0 = 放行。這支當掉是 exit 1，Claude Code 當成
非阻擋的錯誤顯示出來 —— 不會因為這支有 bug 就把所有指令卡死。
"""
import json
import os
import re
import shlex
import subprocess
import sys

SEP = {"&&", "||", ";", ";;", "|", "|&", "&", "(", ")"}
PREFIX = {"env", "command", "sudo", "time", "nohup", "exec",
          "if", "then", "else", "elif", "do", "while", "until", "!", "{"}
GIT_OPT_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
HEREDOC = re.compile(r"(?<!<)<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
DRIVE = re.compile(r"^/([A-Za-z])(/|$)")


def strip_heredocs(cmd):
    """heredoc 和 PowerShell here-string 的內容是資料，不是指令。

    ⚠️ 沒有這段的話，commit 訊息裡一行「git push --force 會…」就會被擋。
    """
    out, end = [], None
    for line in cmd.splitlines():
        if end is not None:
            if line.strip() == end or (end in ("'@", '"@') and line.startswith(end)):
                end = None
            continue
        out.append(line)
        tail = line.rstrip()
        m = HEREDOC.search(line)
        if m:
            end = m.group(1)
        elif tail.endswith("@'") or tail.endswith('@"'):
            end = tail[-1] + "@"
    return out


def native(p):
    """Git Bash 的 /c/Project，Windows 的 Python 要看成 c:/Project 才找得到。"""
    if os.name == "nt":
        p = DRIVE.sub(lambda m: m.group(1) + ":/", p)
    return os.path.expanduser(p)


def commands(cmd, cwd):
    """切成一段一段的指令，順便追蹤 cd —— `cd x && git checkout f` 的 f 在 x 底下。"""
    flat = " ; ".join(strip_heredocs(cmd))
    lex = shlex.shlex(flat, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.escape = ""                     # 不然 C:\Project 的反斜線會被吃掉
    try:
        toks = list(lex)
    except ValueError:                  # 引號沒配對 —— 退回粗切，寧可多看
        toks = flat.split()
    seg = []
    for t in toks + [";"]:
        if t not in SEP:
            seg.append(t)
            continue
        while seg and (seg[0] in PREFIX or ("=" in seg[0] and not seg[0].startswith("-"))):
            seg.pop(0)
        if seg and seg[0] in ("cd", "pushd", "Set-Location") and len(seg) > 1:
            cwd = os.path.join(cwd, native(seg[-1]))
        elif seg:
            yield seg, cwd
        seg = []


def git_args(seg, cwd):
    """`git -C x checkout f` → ("checkout", ["f"], x)。不是 git 就回 None。"""
    if os.path.basename(seg[0]).lower() not in ("git", "git.exe"):
        return None
    i = 1
    while i < len(seg) and seg[i].startswith("-"):
        if seg[i] == "-C" and i + 1 < len(seg):
            cwd = os.path.join(cwd, native(seg[i + 1]))
        i += 2 if seg[i] in GIT_OPT_WITH_VALUE else 1
    if i >= len(seg):
        return None
    return seg[i], seg[i + 1:], cwd


def git(cwd, *a):
    r = subprocess.run(["git", "-C", cwd, *a], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout


def pathspecs(sub, args, cwd):
    """checkout / restore 的參數裡，哪些是「會被蓋掉的檔」。

    ⚠️ `git checkout main` 是切分支，`git checkout README.md` 是還原檔案 ——
       字面上長得一樣。分法跟 git 自己一樣：第一個參數解析得成 commit 就是
       分支（後面的才是檔），解析不成就是檔。
       只看「磁碟上有沒有這個路徑」會誤擋：這個 repo 有 release/ 目錄，
       哪天開了 release 分支，`git checkout release` 就切不過去。
    """
    if "--" in args:
        return args[args.index("--") + 1:]
    if sub == "restore":
        if ({"--staged", "-S"} & set(args)) and not ({"--worktree", "-W"} & set(args)):
            return []                   # 只是取消 stage，工作目錄不動
        src = {args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-s", "--source")}
        return [a for a in args if not a.startswith("-") and a not in src]
    if {"-b", "-B", "--orphan"} & set(args):
        return []
    pos = [a for a in args if not a.startswith("-")]
    if pos and git(cwd, "rev-parse", "--verify", "--quiet", pos[0] + "^{commit}")[0] == 0:
        pos = pos[1:]
    return pos


def modified(paths, cwd):
    """pathspec 裡有未提交修改的檔。一個都沒有的話，還原不會丟東西，就不擋。"""
    _, out = git(cwd, "status", "--porcelain", "--", *paths)
    return [l[3:] for l in out.splitlines() if l and not l.startswith("??")]


def verdict(sub, args, cwd):
    """回傳擋下的理由；放行回 None。"""
    short = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    if sub == "reset" and "--hard" in args:
        return "未提交的修改會直接消失。"
    if sub == "push" and ("f" in short or any(a.startswith(("--force", "+")) for a in args)):
        return "會改寫遠端歷史，別人已經拉下去的 commit 會對不上。"
    if sub == "clean" and ("f" in short or "--force" in args) \
            and "n" not in short and "--dry-run" not in args:
        return "未追蹤的檔會直接刪掉 —— 它們不在 git 裡，沒有地方找得回來。"
    if sub in ("checkout", "switch") and ("f" in short or "--force" in args
                                          or "--discard-changes" in args):
        return "切分支會順便丟掉未提交的修改。"
    if sub in ("checkout", "restore"):
        paths = pathspecs(sub, args, cwd)
        lost = modified(paths, cwd) if paths else []
        if lost:
            return "這些檔有未提交的修改，還原之後救不回來：" + "、".join(lost[:5])
    return None


def main():
    sys.stderr.reconfigure(encoding="utf-8")
    # ⚠️ 讀 bytes 自己解 UTF-8。這台的 console 是 cp950，用 sys.stdin 讀的話
    #    指令裡一有中文（commit 訊息）就解碼失敗 → exit 1 → 非阻擋 → 整支失效。
    call = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    cmd = (call.get("tool_input") or {}).get("command") or ""
    if "git" not in cmd:
        return 0
    for seg, here in commands(cmd, call.get("cwd") or os.getcwd()):
        g = git_args(seg, here)
        why = verdict(*g) if g else None
        if why:
            print("⛔ guard-git 擋下：" + " ".join(seg), file=sys.stderr)
            print("   " + why, file=sys.stderr)
            print("   紅線「不用破壞性 git」（CLAUDE.md）。不要換個寫法繞過去 —— "
                  "停下來說明要做什麼，請使用者自己跑。", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

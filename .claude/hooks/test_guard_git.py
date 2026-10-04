# -*- coding: utf-8 -*-
"""guard-git.py 的回歸測試：該擋的擋、不該擋的放，兩個方向都測。

在臨時 repo 裡跑，不碰這個 repo 的工作目錄。-v 逐條列出。

⚠️ 兩個方向缺一不可。只測「該擋」的話，一支什麼都擋的 hook 也會全過 ——
   然後切分支被擋、commit 被擋，大家很快就把它關掉。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guard-git.py")
VERBOSE = "-v" in sys.argv
NL = chr(10)


def make_repo():
    repo = tempfile.mkdtemp(prefix="guard-")

    def sh(*a):
        subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)

    sh("init", "-q", "-b", "main")
    sh("config", "user.email", "t@t")
    sh("config", "user.name", "t")
    for f in ("README.md", "CLEAN.md"):
        open(os.path.join(repo, f), "w", encoding="utf-8").write("v1" + NL)
    os.makedirs(os.path.join(repo, "release"))
    open(os.path.join(repo, "release", "x.sh"), "w", encoding="utf-8").write("echo" + NL)
    sh("add", ".")
    sh("commit", "-qm", "init")
    sh("branch", "release")                  # 分支跟目錄同名，這個 repo 真的有 release/
    open(os.path.join(repo, "README.md"), "w", encoding="utf-8").write("使用者沒 commit 的修改" + NL)
    open(os.path.join(repo, "untracked.txt"), "w", encoding="utf-8").write("x")
    return repo


def run(cmd, cwd):
    payload = json.dumps({"tool_name": "Bash", "cwd": cwd,
                          "tool_input": {"command": cmd}}, ensure_ascii=False)
    r = subprocess.run([sys.executable, HOOK], input=payload.encode("utf-8"),
                       capture_output=True)
    return r.returncode, r.stderr.decode("utf-8", "replace").strip()


def cases(repo, other):
    r = repo.replace(os.sep, "/")
    block = [
        ("git checkout README.md",                   repo,  "實際踩過的那一條"),
        ("cd " + r + " && git checkout README.md",   other, "追蹤 cd"),
        ("git -C " + r + " checkout README.md",      other, "追蹤 -C"),
        ("git checkout -- .",                        repo,  ""),
        ("git checkout main README.md",              repo,  "先 commit 再檔案"),
        ("git restore README.md",                    repo,  ""),
        ("git reset --hard",                         repo,  ""),
        ("git reset --hard HEAD~1",                  repo,  ""),
        ("git push --force origin main",             repo,  ""),
        ("git push -f origin main",                  repo,  ""),
        ("git push origin +main",                    repo,  "+refspec 也是 force"),
        ("git push --force-with-lease",              repo,  ""),
        ("git clean -fd",                            repo,  ""),
        ("git checkout -f main",                     repo,  ""),
        ("git switch --discard-changes main",        repo,  ""),
        ("git status; git checkout README.md",       repo,  "PowerShell 的 ;"),
        ('git commit -m "中文" && git reset --hard', repo,  "指令含中文（cp950）"),
        ("MSYS_NO_PATHCONV=1 git reset --hard",      repo,  "環境變數前綴"),
        ("echo $(git reset --hard)",                 repo,  "$( ) 裡面"),
    ]
    allow = [
        ("git checkout main",                        repo,  "切分支"),
        ("git checkout release",                     repo,  "分支跟 release/ 目錄同名"),
        ("git checkout -b feature",                  repo,  ""),
        ("git checkout CLEAN.md",                    repo,  "沒有修改，還原不丟東西"),
        ("git restore --staged README.md",           repo,  "只取消 stage"),
        ("git clean -n",                             repo,  ""),
        ("git clean -fn",                            repo,  "-n 是 dry run"),
        ("git push origin main",                     repo,  ""),
        ("git push -u origin main",                  repo,  ""),
        ("git reset --soft HEAD~1",                  repo,  ""),
        ("git log --oneline -3",                     repo,  ""),
        ('echo "git reset --hard"',                  repo,  "引號裡是字串"),
        ("git commit -F - <<'X'" + NL + "git push --force 會改寫歷史" + NL + "X",
                                                     repo,  "heredoc 內容"),
        ("$m = @'" + NL + "git reset --hard" + NL + "'@" + NL + "git status",
                                                     repo,  "PowerShell here-string"),
        ("ls -la",                                   repo,  "沒有 git"),
    ]
    return block, allow


def rm(d):
    def force(func, path, _):
        os.chmod(path, 0o700)                # git 的 object 檔是唯讀，Windows 上刪不掉
        func(path)
    try:
        shutil.rmtree(d, onexc=force)
    except TypeError:                        # Python < 3.12 沒有 onexc
        shutil.rmtree(d, ignore_errors=True)


def main():
    repo, other = make_repo(), tempfile.mkdtemp(prefix="elsewhere-")
    block, allow = cases(repo, other)
    print("guard-git hook，兩個方向")
    bad = 0
    try:
        for want, group, label, miss in ((2, block, "該擋", "漏擋"), (0, allow, "該放", "誤擋")):
            wrong = 0
            for cmd, cwd, note in group:
                code, err = run(cmd, cwd)
                ok = code == want
                wrong += not ok
                if VERBOSE or not ok:
                    shown = cmd.replace(repo.replace(os.sep, "/"), "<repo>").replace(NL, " ⏎ ")
                    print("    %s exit %d  %-44s %s" % ("✅" if ok else "❌", code, shown[:44], note))
                    if not ok and err:
                        print("         stderr:", err[:200])
            print("  %s  %2d 條，%s %d 條" % (label, len(group), miss, wrong))
            bad += wrong
    finally:
        rm(repo)
        rm(other)
    print("❌ 有錯" if bad else "✅ 全部正確")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

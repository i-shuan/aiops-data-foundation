#!/usr/bin/env python
"""檢查 md 的格式規範 —— 粗體、反引號、資訊降維、警告分級。

    python tools/check-md-style.py            只報數量
    python tools/check-md-style.py -v         逐條列出位置
    python tools/check-md-style.py <檔案>...   只看指定檔案

規範本身在 .agents/shared/writing.md 的前四條,這支是它的執法者。

為什麼需要這支
--------------
格式規範寫在 CLAUDE.md 裡只是「希望」—— 沒有東西在守的話,下一個人(或下一個
session)加一段文字牆、在「完全」上面加粗,不會有任何人發現。而格式漂掉的代價
不是醜:**資訊密度掉下來之後,讀的人會開始跳著看,然後漏掉那些「做錯了不會
報錯」的警告** —— 那些警告正是這些文件唯一的價值。

四條規則各自擋什麼
------------------
  ① 粗體用在副詞上
     「**完全**不支援」這種加粗不帶資訊 —— 它強調的是語氣,不是名詞。
     滿版的粗體等於沒有粗體:真正重要的那個「少了就會壞」的關鍵字會被淹掉。

  ② 檔名 / 變數 / 路徑 / 設定 key 沒包反引號
     沒包的話讀的人分不出「這是要照打的字串」還是「這是我在描述它」。
     路徑尤其貴 —— 它會被照著貼進終端機。

  ③ 純文字敘述超過 3 行 / 並列概念超過 2 個還用散文
     中文沒有空格斷詞,連續五行的中文段落是一堵牆。並列的東西用散文寫,
     讀的人要自己在腦子裡建表格。

  ④ 同一節出現兩個以上 blockquote 警告
     警告太多就全部等於沒有。一節留最醒目的那一個,其餘降成行內 ⚠️ 或表格列。

⚠️ 這支**不會**叫你把警告刪掉。警告要留在它描述的那段東西旁邊 —— 收進文末的
   表格會失去上下文,而上下文正是它有用的原因。降級的意思是換呈現方式,
   不是丟掉資訊。
"""

from __future__ import annotations

import io
import os
import re
import sys

# ⚠️ Windows 的中文 console 預設是 cp950,印 ✅ / ❌ 會直接 UnicodeEncodeError
#    —— 檢查做完了,然後死在印結果那一行,退出碼變成 1。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKIP_DIRS = {".git", "node_modules", "dist", "__pycache__", ".pytest_cache", ".work"}

# ---------------------------------------------------------------------------
# ① 粗體裡只有副詞 / 語氣詞
# ---------------------------------------------------------------------------
# ⚠️ 這張表只收**純語氣**的詞。像「不報錯」「沒有預設值」這種是在講事實,
#    加粗是有資訊的,不要加進來 —— 加了這支就會開始誤報,而一個會誤報的
#    檢查很快就會被所有人忽略。
ADVERBS = (
    "完全", "一律", "千萬", "絕對", "根本", "務必", "真的", "整個", "全部",
    "一定", "完完全全", "非常", "極為", "十分", "相當", "特別", "尤其",
    "當然", "顯然", "其實", "本來", "原本就", "從來", "永遠",
)
_ADV_ONLY = re.compile(
    r"\*\*((?:" + "|".join(ADVERBS) + r")(?:不|要|是|沒有|會|沒|沒錯)?)\*\*"
)

# ---------------------------------------------------------------------------
# ② 該包反引號的東西
# ---------------------------------------------------------------------------
_EXT = r"(?:py|tsx?|ya?ml|sh|jsx?|json|md|html|tpl|cfg|ini|toml|lock)"
# ⚠️ lookbehind 一定要排除 `-` —— 不排除的話 `auth-flow.md` 會從連字號後面
#    被切出一個「flow.md」然後報「檔名沒包反引號」。第一版就是這樣,
#    README 一口氣噴 23 處全是誤報。
# ⚠️ 長得像檔名的**產品名**,不是檔名,不要求包反引號。
#    第一版把 `Node.js` 判成「檔名沒包反引號」—— 誤報,而誤報的檢查沒人會理。
_PRODUCT_NAMES = {
    "node.js", "next.js", "vue.js", "nuxt.js", "d3.js", "three.js",
    "express.js", "nest.js", "ember.js", "backbone.js", "chart.js",
}

_BARE = [
    ("檔名", re.compile(r"(?<![\w/`.-])([A-Za-z_][\w-]*\." + _EXT + r")(?![\w`-])")),
    ("路徑", re.compile(r"(?<![\w`/-])((?:applications|platform|services|tests|tools)/[\w./-]{3,})(?![\w`])")),
    ("設定 key", re.compile(r"(?<![\w`-])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,})(?![\w`-])")),
]

# ---------------------------------------------------------------------------
# 把「不該檢查的區域」挖掉:code fence、行內 code、連結目標、HTML 註解
# ---------------------------------------------------------------------------
def mask(text: str) -> str:
    """把 code 區域換成等長的空白,行號和欄位才不會跑掉。"""
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    # ⚠️ fence 一定要 anchor 在行首(`^`),不能只寫 ```.*?```。
    #
    #    CLAUDE.md 第 74 行的行內 code span 裡面就有一個三連反引號
    #    (在講「```bash 區塊」這件事),沒 anchor 的話那個會被當成 fence 開頭,
    #    之後**所有** fence 的配對整排錯開 —— 結果是一個真正的 code block
    #    沒被遮蔽,裡面的目錄樹被當成散文,噴 5 處假的「沒包反引號」。
    #    症狀是誤報,而誤報的檢查沒人會理。
    text = re.sub(r"^```[^\n]*$.*?^```[^\n]*$", blank, text, flags=re.S | re.M)
    text = re.sub(r"^~~~[^\n]*$.*?^~~~[^\n]*$", blank, text, flags=re.S | re.M)
    text = re.sub(r"`[^`\n]+`", blank, text)
    # ⚠️ 要把**整個** [標籤](目標) 挖掉,不是只挖目標。
    #    連結標籤本來就常常是路徑或檔名(`[md/ARCHITECTURE.md](md/ARCHITECTURE.md)`),
    #    它已經是連結了,再包反引號是錯的。第一版只挖目標,README 噴了 23 處
    #    全是誤報 —— 一個會誤報的檢查很快就會被所有人忽略。
    text = re.sub(r"\[[^\]\n]*\]\([^)\n]*\)", blank, text)
    text = re.sub(r"<!--.*?-->", blank, text, flags=re.S)
    # ⚠️ 獨立一行的 `@路徑` 是 Claude Code 的匯入語法,不是散文裡的檔名。
    #    照 ② 包上反引號的話**匯入就失效**(官方:匯入解析會跳過 code span),
    #    Claude Code 安靜地少載入整份檔案 —— CLAUDE.md 的那一行就是這樣,
    #    包了就一條紅線都讀不到。這裡是檢查器讓路,不是人讓路。
    text = re.sub(r"^@[^\s`]+[ \t]*$", blank, text, flags=re.M)
    return text


def strip_bad_examples(lines: list[str]) -> list[str]:
    """把「示範錯誤寫法」的部分挖掉,再交給 ①② 檢查。

    兩種形態:
      · 整行帶 ❌ —— 清單項或單欄表格列,整行不查
      · ❌ / ✅ 對照表 —— **只挖左欄**,右欄(正確寫法)照查

    ⚠️ 沒有這個例外的話,CLAUDE.md 自己的對照表會被自己的規則抓到 ——
       那張表的左欄本來就該長成違規的樣子。第一版噴 3 處誤報。
       規範文件示範反例是正常的,抓它只會讓人去關掉這支檢查。

    ⚠️ 只挖左欄、不整行跳過 —— 整行跳過的話右欄寫錯也抓不到,
       那就變成「規範文件自己可以不守規範」。
    """
    out, in_bad_table = [], False
    for ln in lines:
        is_row = ln.lstrip().startswith("|")
        if is_row and "❌" in ln:
            in_bad_table = True          # 表頭宣告了這是對照表
            out.append(re.sub(r"[^\n]", " ", ln))
            continue
        if not is_row:
            in_bad_table = False
            out.append("" if "❌" in ln else ln)
            continue
        if in_bad_table:
            # 只把第一個資料欄換成空白:| 這裡 | 保留 |
            parts = ln.split("|")
            if len(parts) > 2:
                parts[1] = " " * len(parts[1])
            out.append("|".join(parts))
        else:
            out.append(ln)
    return out


def is_prose(line: str) -> bool:
    """這一行是「純文字敘述」嗎(不是標題 / 表格 / 清單 / 引言 / 空行)。"""
    t = line.strip()
    if not t:
        return False
    if t.startswith(("#", "|", ">", "-", "*", "+", "    ", "\t")):
        return False
    if re.match(r"^\d+[.)]\s", t):
        return False
    if t.startswith(("---", "===")):
        return False
    return True


def check(path: str, rel: str, out: list) -> dict:
    raw = io.open(path, encoding="utf-8").read()
    masked = mask(raw)
    lines = raw.split("\n")
    mlines = masked.split("\n")
    counts = {"bold": 0, "code": 0, "prose": 0, "quote": 0}

    # ①② 查的是「挖掉反例示範」之後的版本;③④ 用原本的(它們看的是結構)
    clean = strip_bad_examples(mlines)

    # --- ① 粗體副詞 ---
    for i, ml in enumerate(clean):
        for m in _ADV_ONLY.finditer(ml):
            counts["bold"] += 1
            out.append((rel, i + 1, "①粗體", f"**{m.group(1)}** —— 副詞/語氣詞不加粗"))

    # --- ② 該包反引號 ---
    for i, ml in enumerate(clean):
        if ml.lstrip().startswith("#"):
            continue  # 標題裡不強制
        for kind, rx in _BARE:
            for m in rx.finditer(ml):
                tok = m.group(1)
                if tok.lower() in _PRODUCT_NAMES:
                    continue
                counts["code"] += 1
                out.append((rel, i + 1, "②反引號", f"{kind} {tok} 沒包反引號"))

    # --- ③ 散文超過 3 行 ---
    run, start = 0, 0
    for i, ml in enumerate(mlines + [""]):
        if is_prose(ml):
            if run == 0:
                start = i + 1
            run += 1
        else:
            if run > 3:
                counts["prose"] += 1
                out.append((rel, start, "③降維", f"純文字敘述連續 {run} 行(上限 3)"))
            run = 0

    # --- ④ 每節至多 1 個 blockquote 警告 ---
    section, sec_line, quotes = "(檔頭)", 1, 0
    in_quote = False

    def flush() -> None:
        nonlocal quotes
        if quotes > 1:
            counts["quote"] += 1
            out.append((rel, sec_line, "④警告",
                        f"「{section}」有 {quotes} 個 blockquote(上限 1)"))
        quotes = 0

    # ⚠️ 「章節」算到**任何層級**的標題(## / ### / ####),不是只算 ##。
    #
    #    規則的意圖是「不要把警告堆在同一個地方」。只算 ## 的話,
    #    md/RELEASE.md 的「第一次上線」會被判 6 個超標 —— 但那 6 個是分散在
    #    七個 ### 步驟裡,每一步一個,本來就是對的形狀。照 ## 算會逼人把
    #    五個警告降級成行內,那一節反而變成一堆 ⚠️ 的雜訊。
    for i, ml in enumerate(mlines):
        if re.match(r"^#{2,4} ", ml):
            flush()
            section, sec_line = ml.lstrip("# ").strip(), i + 1
            in_quote = False
            continue
        if ml.lstrip().startswith(">"):
            if not in_quote:
                quotes += 1
                in_quote = True
        else:
            if ml.strip():
                in_quote = False
    flush()

    return counts


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-v" in sys.argv

    if args:
        targets = [(a, a.replace("\\", "/")) for a in args]
    else:
        targets = []
        for base, ds, fs in os.walk("."):
            ds[:] = [d for d in ds if d not in SKIP_DIRS]
            for f in sorted(fs):
                if f.endswith(".md"):
                    p = os.path.join(base, f)
                    # ⚠️ 不要用 lstrip("./") —— 它會把 ./.github/x.md
                    #    砍成 github/x.md,報出來的路徑是錯的。
                    targets.append((p, os.path.relpath(p, ".").replace("\\", "/")))

    out: list = []
    total = {"bold": 0, "code": 0, "prose": 0, "quote": 0}
    per_file = []
    for path, rel in sorted(targets, key=lambda x: x[1]):
        c = check(path, rel, out)
        for k in total:
            total[k] += c[k]
        if sum(c.values()):
            per_file.append((rel, c))

    print("md 格式檢查(規範見 .agents/shared/writing.md)")
    print(f"{'檔案':<34}{'①粗體':>7}{'②反引號':>9}{'③降維':>7}{'④警告':>7}")
    print("─" * 66)
    for rel, c in sorted(per_file, key=lambda x: -sum(x[1].values())):
        print(f"{rel:<34}{c['bold']:>7}{c['code']:>9}{c['prose']:>7}{c['quote']:>7}")
    print("─" * 66)
    print(f"{'合計':<34}{total['bold']:>7}{total['code']:>9}"
          f"{total['prose']:>7}{total['quote']:>7}")

    if verbose:
        print()
        for rel, line, rule, msg in out:
            print(f"  {rel}:{line} [{rule}] {msg}")

    n = sum(total.values())
    if n:
        print(f"\n❌ 共 {n} 處。加 -v 看位置。")
        return 1
    print("\n✅ 全部符合")
    return 0


if __name__ == "__main__":
    sys.exit(main())

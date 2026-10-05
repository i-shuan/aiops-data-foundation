# Copilot 指令

這個 repo 的常駐規則。完整版見 [`../AGENTS.md`](../AGENTS.md)。

<!-- RULES:START -->
<!-- ⚠️ 正本。CLAUDE.md 用 @ 匯入這份；tools/sync-rules.py 會複製到
     .github/copilot-instructions.md。改這裡，然後跑 `--fix`。 -->

## 紅線（違反就停）

| | |
| --- | --- |
| **不臆測指令** | 只用 `.agents/project/commands.md` 列的。本專案**沒有** `npm` / `make` / `pytest` |
| **機密不進檔案** | 含**使用者貼進對話的** token、密碼。一律改 Secret 參照（`bearerTokenSecret`、`secretKeyRef`） |
| **不用破壞性 git** | `reset --hard`、`push --force`、`clean -fd`。⚠️ 也不要 `git checkout <file>` —— 它會把**未提交的修改一起還原**。Claude Code 會被 `.claude/hooks/guard-git.py` 擋下；Copilot 沒有這層 |
| **不碰正式 cluster** | `kubectl` / `helm` 只對本機 k3d 下，而且要明寫 `--context` / `--kube-context`。正式環境的指令只寫進文件，由人執行 |
| **最小變更** | 單次動 ≤ 3 個檔。不順手重構、不調整無關格式 |
| **不改測試求過** | 測試失敗就貼原始錯誤。動到 `tests/` 要先說 |
| **驗證要貼輸出** | 「應該可以了」不算。chart 渲染得出來**不等於**資料進對 tenant |
| **改了行為就改文件** | 收尾要說動到哪幾份。沒有自動檢查抓得到「敘述過期但關鍵字還活著」 |

## 動手前讀哪個模組

`.agents/` 底下每份都 < 70 行，只放**約束與會壞掉的地方**。
`shared/` 可攜，`project/` 只在本專案成立。

| 要動什麼 | 讀 |
| --- | --- |
| 任何指令、測試、環境 | `.agents/project/commands.md` |
| 寫或改文件 | `.agents/shared/writing.md` |
| 開新需求 | `.agents/shared/workflow.md` |

## 讀大文件之前

給人看的文件是各目錄的 `README.md` 和 `requirements/`。最大的是
`requirements/architecture.html`（約 65 KB，內嵌樣式與程式），不要整份讀。

```bash
grep -n "^## " requirements/elastic-unified-observability/1-intent.md    # 先拿章節行號
```

| 想做什麼 | 怎麼讀 |
| --- | --- |
| 查一個具體的值 | `Grep` |
| 看懂某一節 | `Read` 帶 `offset` / `limit` |
| 整份 | 不要 |

改了 `app/` 或 `platform/` 跑 `./tests/validate.sh`；改了這份跑
`python tools/sync-rules.py --fix`。
<!-- RULES:END -->

# 文件治理：讓文件不再腐化、跟程式脫節、越寫越難讀（2026-10）

**狀態**：完成（`CLAUDE.md` 在新 session 是否生效未驗證，見 Act）　　**日期**：2026-10-29　　**相關**：分支 `claude/docs-governance-run`（計畫先在 `claude/docs-governance`）；前置 [docs-vs-code](2026-10-docs-vs-code.md)（文件對程式一次）
**模組**：跨階段

## Plan：這一次要達到什麼

- **需求**：盤點出 64 份 `docs/` 的 md、另有 13 份放在程式旁邊。問題不是檔案數，是四件事會再發生：
  1. 找不到「這份現在算數嗎」：構想（`graph-rag`）和已做的文件混在一起，沒有狀態。
  2. 同一個主題兩份各說各的：`docs/rca/README.md` 和 `engine/configs/rca/README.md` 都有「三種資料各做什麼」；baseline 在 `docs/rca/` 和 `engine/configs/baseline/` 都講。
  3. 口語說法再冒出來：每次寫新的 PDCA 又會用「跟兄弟比」「保底規則」。
  4. 長文難讀：沒有目錄，原理、參數、操作步驟寫在一起。
- **目的**：每件事用「改一次就不會再發生」的方式處理（索引加狀態、規則寫進會被自動讀的地方、用腳本擋），不是搬完又放著腐化。
- **範圍**：四件事：①全域索引＋狀態標籤 ②設定與原理分離＋長文目錄 ③術語規則寫進 `CLAUDE.md`＋腳本檢查 ④收納 `docs/pdca/` 根目錄的雜項。
- **不做**：不大舉搬檔（`engine/`、`sdk/` 旁的 13 份留原位，避免斷連結、保留 git 歷史）；不改程式；不採 Diátaxis 完整拆四類、也不做「從程式註解自動產生參數文件」（理由見 Act，這次只先做會讓後面更容易做的前置）。

### 調查時發現、跟原本想法不同的四件事（計畫已據此調整）

| 發現 | 影響 |
|---|---|
| **術語表已經存在**：`docs/architecture.md` 附錄「術語對照表 (Glossary)」（431～443 行）有正式術語，其中 **保底規則 → 靜態臨界值規則**、**跟兄弟比 → Pod 同儕橫向比對**、**一把寫入 key → 集中式資料寫入憑證** | 不另立一份「保底 → 兜底」之類的新對照；那樣會跟現有術語打架（「兜底」目前只出現 3 次，「靜態臨界值規則」是 architecture 的正式名）。以現有表為準，並把它搬到 `CLAUDE.md` 當唯一來源 |
| **不是每個口語詞都該換**：「一把」18 次是量詞（一把 key），只有「一把寫入」2 次是舊說法；「一件」122 次、「老師傅」47 次、「值班」39 次是產品用語（賣點就叫老師傅回饋） | 只換表裡列的詞，用精確片語比對，不做全文取代 |
| **最長的文件不是 architecture**：`engine/configs/baseline/README.md` **724 行**，其中「學出來長什麼樣子」120、「自動調整」115、「trace 每一段」80 行都是原理＋參數混著寫 | 長文評估要把它放第一個 |
| **兩階段 roadmap（先一般服務、再 EAP）已經在 master**：`docs/rca/README.md` 第 373 行 | 前面說「可能沒搬過去」是我看錯分支；舊分支 `aiops-baseline` 的內容確實都被取代，可以刪 |

## Do：預計改哪些、為何要改

依序做、各自一個 commit，每個 commit 後跑一次檢查腳本。

### 1. 全域索引＋狀態標籤（`docs/README.md`，新）

- 列全部 77 份 md（`docs/` 64 ＋散落 13），分類：架構與原理／各資料（log、metrics、trace）／根因分析／feedback／操作與排錯／各 engine 設定／PDCA 紀錄。**不搬檔**，連結指到原位。
- 每份標狀態：🟢 已實作（對得到程式與驗證）／🟡 定案方向、部分實作／⚪ 構想未實作。
  - ⚪：`docs/rca/graph-rag.md`（檔頭已寫「構想，還沒做」）。
  - 🟡：`docs/feedback/llm-mlops.md`。**不標成純構想**：它定案走 LLMOps，LLM 摘要已用真的模型跑過（見 phase4），沒做的是評測集、版本管理。
- ⚪／🟡 的檔案開頭加一行醒目的 callout：`> ⚪ 構想，尚未實作；對照程式請看 architecture.md 第 7 節`。
- 這份索引會被腳本檢查：有 md 沒列進去就失敗（見第 3 項），所以不會再變舊。

### 2. 設定與原理分離＋長文目錄

- **`engine/configs/*/README.md` 改成「怎麼跑、參數、輸出在哪、怎麼驗」**，原理段落刪掉、改連結指回 `docs/`。
  | 檔案 | 現況 | 動作 |
  |---|---|---|
  | `engine/configs/rca/README.md`（134 行） | 「三種資料各做什麼」跟 `docs/rca/README.md` 重複 | 刪重複段，保留「怎麼跑、報告長什麼樣子、限制、驗證」 |
  | `engine/configs/baseline/README.md`（**724 行**） | 原理（怎麼判斷、學出來長什麼樣子、跟兄弟比的道理）和參數、指令混在一起 | **先評估再決定**：原理段移到 `docs/rca/baseline.md`（新），configs 留「參數表＋怎麼跑＋結果在哪＋驗證」；評估標準：移完 configs 那份要在 300 行內 |
  | `engine/configs/grafana/README.md`、`engine/deploy/README.md` | 已經是操作文件 | 不動 |
- **長文加目錄**，用 `<!-- toc -->` 標記區塊、由腳本產生（手寫的目錄一定會腐化）：`architecture.md`（444）、`docs/rca/README.md`（394）、`docs/log/README.md`（375）、`baseline/README.md`。
- **只評估、這一輪不拆 `architecture.md`**：master 剛把 poc-status 合進去（「規格在前、現況在後」），拆開等於推翻剛做的決定；先加目錄看夠不夠。

### 3. 術語規則寫進 `CLAUDE.md`（新）＋腳本檢查

- `CLAUDE.md`（現在沒有）放：
  - **術語對照表**（從 `architecture.md` 附錄搬來，那邊改成一行連結，只留一份）。
  - 文件擺放規則：原理放 `docs/`、參數與指令放 `engine/configs/` 旁、PDCA 放對應 phase 資料夾、構想要標 ⚪ 加 callout。
  - 既有的工作慣例（PDCA 四段格式、只合 master 當我說「合回 master」、編號只用 ①～⑨），讓之後不同的人或 session 都照同一套。
- **一定要加腳本**（`tools/check-docs.py`，新）：`CLAUDE.md` 只約束 Claude，人手寫的還是會漏；腳本三件事：
  1. 每份 md 都在 `docs/README.md` 裡。
  2. 相對連結沒斷（這次之前我是臨時寫腳本檢查的，改成留在 repo）。
  3. 活的文件（非 PDCA 紀錄）裡沒有表中的舊說法。
- **替換範圍**：活的文件（`docs/` 的 log、metrics、rca、trace、feedback、architecture、retention、troubleshooting，以及 `engine/`、`sdk/` 旁的 README）換成正式術語。**PDCA 紀錄不改**：它是當時的紀錄，glossary 本來就說「舊紀錄中的口語說法對照」。約 3 個詞、共約 20 處，用精確片語（「跟兄弟比」「保底規則」「一把寫入」）。

### 4. 收納 `docs/pdca/` 根目錄

- `docs-vs-code`、`restructure`、`sonar` → `docs/pdca/meta/`（`meta` 比 `general` 準確：它們講的是專案本身，不是某個 phase）。
- 要同步改的連結：`docs/architecture.md`（397、398 行）、`docs/pdca/README.md`（167～169 行）、`sonar-project.properties` 的註解，以及搬過去的三份自己的相對連結（多一層 `../`，其中一份有）。用 `git mv` 保留歷史。
- 這份計畫自己（`2026-10-docs-governance.md`）執行完也搬進 `meta/`，並在 `docs/pdca/README.md` 加一列。

## 實際做了什麼、跟計畫不同的地方

- 順序是 檢查腳本 → ① → ② → ③ → ④，每項一個 commit；腳本先做，才量得到「執行前的基準」。
- **索引分兩層**：`docs/README.md` 列非 PDCA 的文件（及連到 `docs/pdca/README.md`），54 份 PDCA 紀錄由原本的 `docs/pdca/README.md` 當索引；腳本兩層都檢查。不在 `docs/README.md` 重複列 54 份。
- **baseline 原理只搬了一小部分**：搬走「為什麼用中位數和 MAD」「MAD 一步一步算」「抓不到的」共約 50 行到 `docs/rca/baseline.md`。其餘章節（trace 每一段、規則、同儕比對、調整、feedback、慢性病、資料品質）原理和參數交錯在同一個段落，硬拆要重寫、會斷 20 幾個章節連結，所以退回計畫裡的備案：加目錄。`engine/configs/baseline/README.md` 724 → 715 行，**≤ 300 行的目標沒達到**。
- 舊說法只換 md（9 處）；程式裡的註解和 docstring（約 20 處，`peer.py`、`gen-cronjobs.py`、測試等）沒動，因為不是文件，且 `gen-cronjobs.py` 會影響產生的 CronJob。

## Check：測試的目的與結果

| 目的 | 怎麼驗 | 結果 |
|---|---|---|
| 沒斷連結（含章節錨點） | `tools/check-docs.py`，執行前量基準、每項之後各跑一次 | 前後都是 0 個斷；改標題「跟兄弟比」→「Pod 同儕橫向比對」後 4 個錨點一併更新；搬 3 份（加計畫自己）進 `meta/` 後 6 個連結都修好 |
| 索引完整 | 腳本：`git ls-files '*.md'` 對兩層索引 | 執行前 `docs/README.md` 不存在；現在 81 份 md 全在索引裡，0 個缺 |
| 術語乾淨 | 腳本：活的文件搜「跟兄弟比」「保底規則」「一把寫入」 | 執行前 11 處（含 3 處術語表本身）→ 0；PDCA 紀錄不算 |
| 沒誤傷 | 換之前 `git grep` 列出全部命中；「一件」「老師傅」「值班」「一把」（量詞）不在替換清單 | diff 只有表內詞；老師傅等維持原樣 |
| 沒改到程式 | `pytest engine/tests`；`git diff --name-only master..HEAD` 看非 md 檔 | 346 passed；非 md 只有新增的 `tools/check-docs.py` 和 `sonar-project.properties` 的一行註解 |
| 原理搬走沒搬壞 | 搬完腳本檢查；搬過去的連結改成新位置的相對路徑 | `docs/rca/baseline.md` 內的連結都通 |
| 目錄不會腐化 | `check-docs.py --toc` 重新產生；目錄裡的連結同樣被連結檢查驗過 | 4 份長文都有；改標題後重跑就更新 |
| `CLAUDE.md` 真的有效 | 派一個子 agent，不給提示、問 PDCA 路徑、用詞、提交前指令 | **沒驗到**：子 agent 回答「不知道」，因為 `CLAUDE.md` 是 session 開始之後才新增的，已經在跑的 agent 不會重讀。要在**新 session** 驗（見 Act） |

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `CLAUDE.md` 是否被自動讀到、是否真的讓新文件用正式術語，沒驗證 | 子 agent 檢查（上面） | 這是第 3 項最核心的承諾，目前只有檢查腳本那一半有驗證 | 合進 master 後開一個新 session，不給提示請它寫一份假的 PDCA，看路徑、用詞、提交前指令 |
| `engine/configs/baseline/README.md` 仍有 715 行 | 拆分評估 | 原理與參數交錯，難讀的問題只靠目錄緩解 | 若要再拆，要逐章重寫（不是搬）；先看目錄夠不夠用，再決定 |
| 程式註解、docstring 還有「跟兄弟比」約 20 處 | 全 repo `git grep` | 不影響文件；但新人讀程式會看到舊說法 | 值得的話另開一輪連同 `gen-cronjobs.py` 重新產生 CronJob |
| 腳本只擋三個片語 | 設計 | 表裡其他舊說法（「學平常的樣子」「吞例外」等）靠人看 | 看哪些真的反覆出現，再逐個加進 `FORBIDDEN` |
| 腳本沒接 CI | repo 沒有 CI | 不跑就不擋 | 有 CI 時加一行；或加 git pre-commit hook |

- **決定**：照計畫做完四項；baseline 長文退回「加目錄」；`CLAUDE.md` 生效與否留給新 session 驗。
- **下一輪**：合進 master 後驗 `CLAUDE.md`；視需要把腳本接進 CI。

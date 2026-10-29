# 文件治理：讓文件不再腐化、跟程式脫節、越寫越難讀（2026-10）

**狀態**：計畫（還沒改任何既有檔案；這份是唯一新增的檔案）　　**日期**：2026-10-29　　**相關**：分支 `claude/docs-governance`；前置 [docs-vs-code](2026-10-docs-vs-code.md)（文件對程式一次）
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

## Check：預計怎麼驗證

| 目的 | 怎麼驗 | 預期 |
|---|---|---|
| 沒斷連結 | `tools/check-docs.py` 的連結檢查（搬完前後各跑一次） | 0 個斷的；搬完前先量現況當基準 |
| 索引完整 | 腳本：全 repo `git ls-files '*.md'` 對索引 | 77 份（＋新增的 `docs/README.md`、`CLAUDE.md`、`baseline.md`）全在 |
| 術語乾淨 | 腳本：活的文件裡搜「跟兄弟比」「保底規則」「一把寫入」 | 0 個；PDCA 紀錄不算 |
| 沒誤傷 | 換之前先 `git grep` 列出全部命中，逐筆看上下文；「一件」「老師傅」「值班」「一把」（量詞）不能被動到 | 換完 diff 只有表內詞 |
| 沒改到程式 | `pytest engine/tests` | 346 passed（跟現在一樣） |
| 拆原理有沒有拆壞 | `baseline` README 搬完後：configs 那份行數、`docs/rca/baseline.md` 的連結都通 | configs ≤ 300 行；所有 `README#章節` 的連結仍指得到 |
| `CLAUDE.md` 真的有效 | 新 session 請它寫一份假的 PDCA，看它用不用正式術語、放不放對資料夾 | 用正式術語；放對 |

## Act：還沒決定、這一輪不做的

| 項目 | 為什麼不在這一輪 | 要你決定／後續 |
|---|---|---|
| **PDCA 紀錄裡的舊說法要不要也改** | 它們是歷史紀錄，glossary 已經在處理；全改會產生大量 diff、蓋掉當時的措辭 | 預設不改。你若要全改，只是把腳本的檢查範圍放大 |
| **Diátaxis 完整四象限** | 需要整批重分類、搬檔，會斷連結；這個 repo 的文件已經大致分成「原理（docs）」「操作（configs、deploy）」「案例（cases、scenarios）」，缺的是狀態標籤和重複的清理 | 第 1～2 項做完後再看；用索引的分類欄就是它的雛形 |
| **「從程式註解自動產生參數文件」** | 現在 config 範例是 YAML 加註解，沒有 schema；要先有 schema 才能抽取，是另一個比較大的工作 | 第 2 項把「參數表」從原理分出來，就是它的前置；有需要再開一輪，先挑 `rca.yaml`、`baseline.yaml` 試 |
| `architecture.md` 要不要拆 | 見上：剛合進來，先加目錄 | 加目錄後再看 |
| 重複的 `engine/configs/baseline` 原理搬到 `docs/rca/baseline.md` 會動到很多 `README#章節` 連結 | 是計畫裡風險最大的一步 | 先跑連結檢查量基準；搬完逐一修；若斷太多就退回「只加目錄」 |

- **決定**：不搬散落的 13 份、不動 PDCA 紀錄的措辭、不拆 `architecture.md`；其他四件照上面做。
- **下一輪**：你確認這份計畫後，依第 1→4 項順序執行，每項一個 commit。

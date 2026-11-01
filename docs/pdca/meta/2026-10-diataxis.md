# 共用文件採用 Diátaxis：每份文件只做一種事（2026-10）

**狀態**：完成（每個章節的歸類是我逐一判斷的，沒有第二個人複核；不拆的 9 份只看主要類型）　　**日期**：2026-10-29　　**相關**：分支 `claude/diataxis`（從 `claude/docs-governance-run` 開出，因為索引、`CLAUDE.md`、檢查腳本都在那條線上；那條線還沒合進 master）；前置 [docs-governance](2026-10-docs-governance.md)
**模組**：跨階段

## Plan：這一次要達到什麼

- **需求**：長文難讀的原因是同一份文件裡混了四種東西：解釋「為什麼」（原理）、教人「怎麼做」（操作）、讓人「查」（規格）、帶著走一遍的實例（案例）。例如 `docs/log/README.md` 401 行，欄位規範、pipeline 規則、部署步驟、設計原理寫在同一份。
- **目的**：每一份共用文件只屬於一種類型，一眼看得出類型，找參數的人不用滑過原理，看原理的人不被步驟打斷。
- **範圍**：`docs/` 底下非 PDCA 的 17 份共用文件（architecture、retention、troubleshooting、log、metrics、trace、rca、feedback）。
- **不做**：
  - PDCA 紀錄（它是當時的紀錄，不是文件）。
  - `engine/`、`apps/`、`e2e/` 旁邊的 13 份 README：它們本來就貼著程式、是操作與規格；只在索引標類型，不拆。
  - 不改程式。

### 四種類型

| 類型 | 回答 | 這個 repo 裡長什麼樣 |
|---|---|---|
| 原理 Explanation | 為什麼這樣設計？ | `architecture.md`、各主題 `README.md`、`rca/`、`fluentbit-vs-otel.md`、`llm-mlops.md` |
| 操作 How-To | 我要做 X，怎麼做？ | `how-to.md`、`troubleshooting.md` |
| 規格 Reference | 查：欄位、參數、現況對照 | `reference.md`、`catalog.md`、`architecture-reference.md` |
| 案例 Cases | 實際跑一遍長什麼樣？ | `cases.md`、`scenarios.md` |

案例不叫 Tutorials：Diátaxis 的 Tutorials 是帶初學者學習的教學，這裡是實測與場景，不是教學。

### 怎麼拆（原則）

1. **檔案留在原資料夾**，用檔名分類型（`README.md` 原理、`reference.md`、`how-to.md`），不另開 `explanation/`、`how-to/` 四個頂層資料夾。這是前一輪就定的「不大搬檔」原則（連結、git 歷史、程式註解裡的路徑都不斷）；要看「依類型」的導覽，在 `docs/README.md` 用類型欄與分組給。
2. **以 `##` 章節為單位**搬，章節內容不改寫；原標題不變，所以章節錨點不變，連結由腳本統一改到新檔案。
3. 每份共用文件第一行後有一行類型標示（`> **類型：…**`）和同主題文件的導覽；檢查腳本會擋沒標的。
4. **「現況與待辦」「還沒做」留在原理文件**：Diátaxis 沒有「狀態」這一類，放在原理（解釋現在走到哪、為什麼還沒做）最接近。這是刻意的折衷。
5. 每個章節的歸類看內容，不看標題：「為什麼這樣定」是原理；「部署」「驗證」「坑」是操作；「欄位」「pipeline 規則」「檔案」是規格。

## Do：預計改哪些、為何要改

| 動作 | 檔案 |
|---|---|
| 拆成 原理／規格／操作 | `log/README.md`、`metrics/README.md`、`trace/README.md`、`retention.md`、`metrics/dotnet.md`、`metrics/catalog.md`、`architecture.md`（第一部分規格 → 原理；第二部分現況與驗證 → 規格） |
| 只標類型、不拆 | `troubleshooting.md`（操作）、`log/fluentbit-vs-otel.md`、`rca/README.md`、`rca/baseline.md`、`rca/graph-rag.md`、`feedback/README.md`、`feedback/llm-mlops.md`（原理）、`metrics/cases.md`、`rca/cases.md`、`trace/scenarios.md`（案例） |
| 改連結 | 全 repo 的 md：指到被拆文件的連結（含章節錨點）改指新位置；文字裡的「第 N 節」改成指到新檔 |
| 索引 | `docs/README.md` 加類型欄、依類型分組；`CLAUDE.md` 寫類型與檔名規則；`tools/check-docs.py` 檢查每份共用文件有類型標示 |

## Check：預計怎麼驗證

| 目的 | 怎麼驗 | 預期 |
|---|---|---|
| 內容沒掉 | 拆前拆後，所有章節標題與行數（扣掉新加的類型標示、目錄）逐一對 | 每個原章節都剛好出現在一個新檔，行數一致 |
| 沒斷連結 | `tools/check-docs.py` 的連結檢查（含章節錨點），拆前量基準、拆後再跑 | 0 個斷 |
| 每份只有一種類型 | 逐檔人工看章節清單（對照上面的歸類原則） | 沒有混的；有疑義的列在 Act |
| 索引、類型標示齊 | 腳本 | 缺的 0 個 |
| 沒改到程式 | `pytest engine/tests`；`git diff --name-only` 看非 md | 346 passed；只有 `tools/check-docs.py` |
| 程式註解裡指到文件的路徑 | `git grep 'docs/' -- '*.py' '*.yaml' '*.sh'`，看有沒有指到被拆的章節 | 列出，能改的改 |

## 實際做了什麼、跟計畫不同的地方

- **7 份混合文件拆成 19 份**（含原檔位置保留的那份）：

  | 原檔 | 拆成 |
  |---|---|
  | `log/README.md`（401 行） | 原理 `README.md`（65）、規格 `reference.md`、操作 `how-to.md` |
  | `metrics/README.md` | 原理 `README.md`、規格 `reference.md`、操作 `how-to.md` |
  | `metrics/catalog.md` | 規格 `catalog.md`、原理 `capabilities.md`（「metrics 能做到什麼」） |
  | `metrics/dotnet.md` | 原理 `dotnet.md`、操作 `dotnet-how-to.md`、規格 `dotnet-reference.md`；「實測結果」併入案例 `cases.md` |
  | `trace/README.md` | 原理 `README.md`、規格 `reference.md`、操作 `how-to.md` |
  | `retention.md` | 原理 `retention.md`、規格 `retention-reference.md`、操作 `retention-how-to.md` |
  | `architecture.md` | 原理 `architecture.md`（第一部分：§1～5）、規格 `architecture-reference.md`（第二部分：§6～14、附錄）；章節編號不變 |

- **計畫沒預料到、做到一半補的**：以 `##` 拆完後，`log/reference.md` 的 `pipeline` 章節裡還夾著三個操作小節（服務怎麼宣告格式、新增一種格式、查哪些格式還沒有規則），`retention.md` 裡夾著兩個（量太大時怎麼降、怎麼量正式環境的量）。把這 5 個小節再搬進各自的 `how-to`，並更新 3 條指到它們的錨點。
- 程式與設定檔裡有 14 處註解指到被拆的文件章節（`peer.py`、`rules.py`、`_deployment.tpl`、`pipeline.json` 的 description 等），一併改到新位置。**只改註解文字**；`pipeline.json` 的 description 是部署到 ES 的說明文字，下次跑 `elasticsearch-setup.sh` 會更新，不影響 pipeline 行為。
- 拆檔用一次性的腳本（不放進 repo）：以章節為單位搬、不改寫內容；原標題不變所以錨點不變，連結由腳本統一改。

## Check：測試的目的與結果

| 目的 | 怎麼驗 | 結果 |
|---|---|---|
| 內容沒掉 | 拆前拆後，每份來源的所有非空白行（連結目標、層級 `##`→`###` 不計）逐行對照 | 沒有遺失內容；少的只有：改過的 H1 標題、「第一部分／第二部分」分隔用的 H1、被改名的章節標題、被我重寫的導言、一處「第 4 節」改成帶連結的寫法、4 條因標籤文字隨目標改名而不同的連結 |
| 沒斷連結（含章節錨點） | `tools/check-docs.py`；**第一次跑漏了新檔**（腳本只看 git 已追蹤的檔案，新檔沒 `git add` 就沒被掃到，當時顯示 0 個斷是假的），`git add` 後重跑才抓到 3 個斷（拆檔腳本沒處理新檔裡的連結、標籤還寫舊檔名），修完 | 0 個斷 |
| 每份只有一種類型 | 逐檔列出章節清單人工看 | 拆的 7 份：發現上面 5 個夾帶的操作小節，搬走後沒有混的。不拆的 9 份只看主要類型，沒逐段審（見 Act） |
| 類型標示齊 | `check-docs.py` 新增的類型檢查 | 31 份共用文件都有；缺或寫錯會失敗 |
| 索引齊 | 腳本 | 94 份 md 全在索引裡；`docs/README.md` 重寫成「依主題」加「依類型找」兩種看法 |
| 程式沒壞 | `pytest engine/tests`；`git diff` 看非 md 的變動 | 346 passed；非 md 只有註解文字（14 處）和檢查腳本 |
| 註解裡的路徑還對 | `git grep 'docs/…'` 對照新檔案 | 剩下指到 `README.md` 的都是原理（設計說明），確認過 |

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 不拆的 9 份只標主要類型，沒逐段審 | 工作量；`rca/README.md`（427 行）有「現況與待辦」「每一步要的資料，現在準備到哪裡」「建議順序」這類狀態與計畫小節，`feedback/README.md` 也有 | 這幾份仍可能夾帶非原理的段落 | 狀態類小節按計畫原則 4 留在原理；若要更嚴，再開一輪逐段審 |
| `architecture-reference.md`（規格）裡的 §8 收斂、§9 產品主線 8 步、§11 哪裡過度設計其實是原理或敘述 | 照原本「第二部分＝現況與驗證」整份歸規格 | 不是純查表 | 保留原本兩部分的切法（master 剛決定的），要更純可再把 §8、§11 移到 `architecture.md` |
| `engine/configs/baseline/README.md`（715 行）仍是規格＋操作＋原理混在一起 | 它在程式旁邊，不在這一輪範圍 | 最長、最混的一份還在 | 下一輪最值得拆的一份 |
| `docs/README.md`、各檔的導覽列要手動維護 | 設計 | 新增文件時要記得改 | 檢查腳本會擋沒列進索引、沒標類型的；導覽列靠人 |
| 「案例」不是 Diátaxis 的 Tutorials | 我們的案例是實測與場景，不是帶初學者學習的教學 | 如果之後要寫真正的教學（第一次部署一個應用），要另開 `tutorials` 類型 | 目前沒有這類需求 |
| 拆檔腳本是一次性的，沒進 repo | 設計 | 之後要再拆別的文件得重寫 | 若會常拆再收進 `tools/` |

- **決定**：照計畫做完；檔案留在原資料夾用檔名分類型，不另開四個頂層資料夾。
- **下一輪**：拆 `engine/configs/baseline/README.md`；逐段審不拆的 9 份。

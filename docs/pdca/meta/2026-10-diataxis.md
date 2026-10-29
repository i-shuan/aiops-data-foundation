# 共用文件採用 Diátaxis：每份文件只做一種事（2026-10）

**狀態**：進行中　　**日期**：2026-10-29　　**相關**：分支 `claude/diataxis`（從 `claude/docs-governance-run` 開出，因為索引、`CLAUDE.md`、檢查腳本都在那條線上；那條線還沒合進 master）；前置 [docs-governance](2026-10-docs-governance.md)
**模組**：跨階段

## Plan：這一次要達到什麼

- **需求**：長文難讀的原因是同一份文件裡混了四種東西：解釋「為什麼」（原理）、教人「怎麼做」（操作）、讓人「查」（規格）、帶著走一遍的實例（案例）。例如 `docs/log/README.md` 401 行，欄位規範、pipeline 規則、部署步驟、設計原理寫在同一份。
- **目的**：每一份共用文件只屬於一種類型，一眼看得出類型，找參數的人不用滑過原理，看原理的人不被步驟打斷。
- **範圍**：`docs/` 底下非 PDCA 的 17 份共用文件（architecture、retention、troubleshooting、log、metrics、trace、rca、feedback）。
- **不做**：
  - PDCA 紀錄（它是當時的紀錄，不是文件）。
  - `engine/`、`sdk/`、`e2e/` 旁邊的 13 份 README：它們本來就貼著程式、是操作與規格；只在索引標類型，不拆。
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

## Act：（執行後填）

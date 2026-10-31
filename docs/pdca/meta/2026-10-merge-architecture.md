# architecture.md 與 architecture-reference.md 合成一份（2026-10）

**狀態**：完成　　**日期**：2026-10-31　　**相關**：分支 `claude/merge-architecture-docs`（從 master `831e471` 開出）
**模組**：跨階段

## Plan：這一次要達到什麼

- **需求**：同一個主題放在兩份文件（`architecture.md` 第 1～5 節的設計、`architecture-reference.md` 第 6～14 節的現況與驗證），章節編號接續、兩份互相指來指去（第 8 節連回第 4 節、`architecture.md` 的取捨連到 `architecture-reference.md` 的附錄 A.4），讀一個主題要翻兩個檔。
- **目的**：只剩 `docs/architecture.md` 一份，內容不增不減；所有指到 `architecture-reference.md` 的連結改指新位置，`tools/check-docs.py` 0 個問題。
- **範圍**：合併兩份、改連結與索引。不改內容的說法與數字；PDCA 紀錄只改連結目標，當時的措辭不動。

## Do：改動哪些 code、改了什麼、為何要改

沒有程式邏輯的改動；`engine/tests/loop_e2e.py` 只改 docstring 裡的檔名。

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `docs/architecture.md` | 第 1～5 節（原 `architecture.md`）接上第 6～14 節與附錄（原 `architecture-reference.md`）；標題改成「架構、設計決策與現況」，開頭兩段併成一段，目錄重新產生；原本指向 `architecture.md` 第 4 節的連結改成頁內連結 | 章節編號本來就接續（第 6～14 節是從同一份拆出去的），合回去不用改編號，既有的 `#9-產品主線-8-步` 這類錨點都不會變 |
| `docs/architecture-reference.md` | 刪除 | 內容已併入 `architecture.md` |
| `docs/README.md` | 拿掉 `architecture-reference.md` 兩處（總覽表一列、「查欄位、參數、現況」一列）；`architecture.md` 那列的說明補上現況與 PDCA 索引 | 索引要跟著檔案增減（`CLAUDE.md`：新增、刪除、改名 md 同一個 commit 更新） |
| `README.md`、`docs/feedback/README.md`、`docs/feedback/llm-mlops.md`、`docs/rca/graph-rag.md`、`engine/configs/rca/README.md`、`engine/tests/loop_e2e.py` | 檔名與連結目標 `architecture-reference.md` → `architecture.md` | 活的文件指到新位置 |
| `docs/pdca/` 底下 11 份紀錄的 12 個連結 | 只改連結目標（括號裡）；連結文字與當時的措辭不動 | 不然 `check-docs.py` 報連結斷 |

類型標示用「原理 Explanation」：原本 `architecture.md` 的類型；`CLAUDE.md` 也寫「現況與待辦」留在該主題的原理文件（Diátaxis 沒有「狀態」這一類，放在原理最接近），所以第 6～14 節的現況與驗證放進來不違反「一份一種」。

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 兩份的內容沒有漏 | 把兩份原文（從 `git show origin/master:` 取）第 1 節、第 6 節起的每一行非空白行，逐行找在合併後的檔案裡有沒有 | 只有 1 行不同，是刻意改成頁內連結的那行（第 8 節表格第一列），其餘 0 行缺 |
| 沒有斷掉的連結、索引完整、沒有舊說法、類型標示合格 | `python3 tools/check-docs.py`（含 `--toc` 重新產生目錄） | 四項都是 0 個問題；全 repo 指到 `architecture-reference.md` 的連結剩 0 個（還出現這個檔名的，只有 PDCA 紀錄與本份裡用程式碼格式寫的檔名，是當時的說法） |
| 錨點沒變 | 第 6～14 節標題原樣保留；`check-docs.py` 檢查連結的錨點 | 原本指到 `#9-產品主線-8-步`、`#8-收斂…`、`#10-還沒做…` 的連結都還有效 |

- **沒測到的**：沒有在 GitHub 上看 mermaid 圖與目錄的呈現；`engine` 的單元測試沒跑（只改了 docstring 的檔名）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| `architecture.md` 變成 460 多行，前 5 節（既定設計）和後 9 節（現況，常改）混在同一份 | 合併後數行數 | 現況表更新時，PR 的差異會跟設計段落混在一起；2026-10-docs-governance 當初「規格在前、現況在後」的做法就是這樣 | 現況變動多到干擾時再拆；目前不處理 |

- **決定**：合併時不重新編排章節。編號、錨點全部保留，之後想再拆也能照 §5／§6 的界線切開。

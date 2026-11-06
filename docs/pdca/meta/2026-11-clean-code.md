# 程式整理：先順主軸，再拆、再理依賴、再改型別（2026-11）

**狀態**：進行中　　**日期**：2026-11-06　　**相關**：分支 `claude/clean-code`（從 `claude/vo-models` `d169924` 開，含 `Incident` 模型）；取代 [vo-models](2026-11-vo-models.md) 的步驟 2～6
**模組**：②～⑨（程式結構，不改任何階段的行為）

## Plan：這一次要達到什麼

- **需求**：使用者看程式覺得「越來越發散、越來越多、很髒」。量過（`tests` 以外 85 檔、12,463 行、734 個函式）：
  1. **找不到**：`detector/` 31 檔同時放 ②③④⑤ 和整個 ⑨（feedback、tune、adjust、backtest、metrics、incident）；⑨ 又散在 `detector/`、`investigator/importer.py`、`api/` 三處；`aiops-incidents` 一個索引被三個資料夾各寫一份。文件用 ①～⑨，程式用 detector／investigator，兩套名字。
  2. **讀不懂**：`cmd_feedback` 168 行用 `if args.action ==` 塞 6 個命令；`cmd_alert.run` 117 行、巢狀 5 層；函式 >50 行 13 個、巢狀 ≥4 層 28 個；`detector/__main__.py` 的 `adjust` 24 個參數、`feedback` 18 個，用 `locals()` 整包丟成 `Namespace`。
  3. **改不動**：套件互相 import（`core`→`investigator`、`core`→`sources`、`sources`→`detector`／`investigator`、`detector`↔`investigator`），靠 71 處函式內 import 和「把函式當參數傳進來避免循環」硬解；`except Exception` 26 處。
  4. **沒型別**：`cfg` 是 dict 傳遍全專案（`cfg.get` 166 處）、`.get("欄位")` 1,431 處；只有 `Anomaly`、`Incident` 和設定是模型。
- **目的**（順序就是重要度，前一步不做後一步會白做）：
  - **A** 一個階段一個資料夾：⑨ 全部搬進新的 `feedback/`；`detector/` 只剩 ②③④⑤、`investigator/` 只剩 ⑥⑦⑧；每個套件的 docstring 第一行寫它是哪幾個模組；CLAUDE.md 結構表與 architecture 4.2 對應。**命令列名稱不變**（`python3 -m detector feedback …` 照舊：排程表、helm、64 份文件都在用），`detector/__main__.py`、`investigator/__main__.py` 只是入口（組合根），從 `feedback/` 掛子命令。
  - **B** 讀得懂：沒有函式 >100 行；`action` 當參數的命令（feedback、adjust、incident）改成 typer 子命令，每個子命令只有自己的參數；不再 `locals()`。
  - **C** 依賴只往一個方向：`core` → `sources` → `detector`、`investigator` → `feedback` → `api`、`scheduler`、`tasks`、`plugins`、`models`（門面）、`__main__`。用 `tests/test_layers.py` 鎖住，之後誰加反向 import 測試就紅。函式內 import 只留真的要 lazy 的（SQLAlchemy、LLM SDK）。
  - **D** 跨階段的文件有型別：`Anomaly`（已有，搬 `core/`）、`Alert`（⑥ 寫、⑧⑨ 讀）、`aiops-tuning` 的調整文件（⑨ 寫、④ 讀）；規則同 [vo-models](2026-11-vo-models.md)：`to_doc()` 跟改前逐欄位相同、`from_doc()` 讀舊文件不炸、mapping 跟模型對照測試。
- **範圍**：
  - 做：上面 A～D；每一步一個 commit、全部單元測試過才下一步。
  - 不做：改任何階段的行為、ES 查詢語句、文件格式、命令列參數名；`cfg` 改成到處傳模型（226 處，另開一輪）；`Span`／`Findings`（⑦ 內部的 dict，不跨階段）；PDCA 紀錄裡的舊路徑措辭（只修會斷的連結）。

## Do：改動哪些 code、改了什麼、為何要改

（每一步做完補）

## Check：測試的目的與結果

（每一步做完補）

## Act：Check 後沒改善的、還沒解決的

（最後補）

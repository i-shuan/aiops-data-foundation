# detector 的指令進入點拆檔（2026-10）

**狀態**：已完成　　**日期**：2026-10-25　　**相關**：分支 `claude/split-detector-cli`
**模組**：④⑤⑨（只是整理程式，不改行為）

## Plan：這一次要達到什麼

- **需求**：`engine/detector/__main__.py` 有 1,100 行，13 個指令的邏輯全在同一個檔。
- 13 個指令：train、detect、tune、adjust、rules、peer、quality、chronic、feedback、incident、trace-*。
- 接下來告警出口、回饋入口、RAG 都要往這裡加，再加就更難讀、改一處怕弄壞別處。
- **目的**：一個指令（或一組相關指令）一個檔，`__main__.py` 只剩參數解析和分派。
- **行為一字不改**：單元測試照過；`python3 -m detector <指令>` 的用法、輸出、寫進 ES 的東西都一樣。
- **範圍**：只搬程式碼、補 import；不改函式內容、不改名（底線開頭的如 `_now` 也不改，減少差異量）。不碰 `investigator/`。

| 新檔（`engine/detector/`） | 放什麼 |
|---|---|
| `common.py` | 共用小工具：`keys_of`、`_freq`、`_now`、`_v`、`RULE_FIELDS` |
| `tuning.py` | `aiops-tuning` 的 index、mapping 與三個 load 函式 |
| `cmd_baseline.py` | train、detect、tune 和它們的輔助 |
| `cmd_adjust.py` | adjust |
| `cmd_feedback.py` | feedback、incident |
| `cmd_chronic.py` | chronic |
| `cmd_trace.py` | trace-collect／train／detect |
| `cmd_rules.py`、`cmd_peer.py`、`cmd_quality.py` | 各一個 |
| `__main__.py` | 說明、argparse、分派表 |

- `tuning.py` 的三個函式：`load_adjustments`、`load_overrides`、`data_adjustments`。
- `RULE_FIELDS`：規則、peer、quality、trace-detect 寫異常總表時補的欄位。
- `cmd_baseline.py` 的輔助：存 baseline、存異常、選粒度、借 baseline、status。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/detector/__main__.py` | 1,100 行 → 160 行：模組說明、`main()` 的 argparse 與分派表 | 進入點只做「讀參數、呼叫誰」 |
| `common.py`（新，22 行） | 見上表 | 每個 cmd 都用 |
| `tuning.py`（新，30 行） | `TUNING_INDEX`、`TUNING_MAPPING` 與三個 load 函式 | baseline、adjust、feedback、trace 四邊都讀 `aiops-tuning` |
| `cmd_baseline.py`（新，349 行） | train、detect、tune 和共用輔助 | 三個指令共用輔助，拆開反而要互相 import |
| `cmd_adjust.py`、`cmd_feedback.py`、`cmd_chronic.py`（新） | 各自的指令 | 一個指令一個檔 |
| `cmd_rules.py`、`cmd_peer.py`、`cmd_quality.py`（新） | 各自的指令 | 同上 |
| `cmd_trace.py`（新） | trace 指令；共用 `cmd_baseline` 的四個函式（見下） | 同上 |
| `detector/es.py` | 只改一行註解（`doc_id` 的 `noqa` 說明） | ruff 的 noqa 後面緊接全形括號時認不得 |

- `cmd_trace` 用的：`save_baseline`、`save_anomalies`、`with_status`、`train_summary`。

**怎麼搬**：用 AST 腳本切。

- 每個頂層函式／常數連同緊貼在上面的註解，搬到指定的檔。
- 每個新檔的 import 由「這段程式用到哪些名字」反推（從原本的 import 區塊過濾）。
- 跨檔引用自動補 `from .xxx import …`。
- 函式內容一個字沒動，名字也沒改。

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 沒少／多 import、沒用到未定義的名字 | `ruff check --select F detector/` | 0 個問題 |
| 計算邏輯沒變 | `pytest`（200 個） | 全過 |
| e2e 測試的進入點不用改 | 3 個 e2e 測試用 `from detector.__main__ import main as cli` | 照常可用 |
| 每個指令跑得起來（import、分派、讀設定、連 ES） | 對 es-t 跑 9 個指令（見下） | 都正常結束、輸出格式跟拆之前一樣 |
| `--help` 的子指令清單 | `python3 -m detector --help` | 13 個子指令跟拆之前一樣 |

- 已修：`__main__.py` 結尾 `sys.exit(main())` 的 `sys` 沒 import（反推 import 只看了 `main()` 本身）。
- 實跑的 9 個：`rules`、`quality`、`adjust list`、`feedback summary`、`incident list`、`detect`、`peer`、`chronic`、`trace-detect`。
- es-t 的測試資料已超過時間窗，觸發 0 個；這項只看跑不跑得起來。
- **沒測到**：`train`、`tune`、`trace-collect`、`trace-train` 沒對 ES 實跑（單元測試有涵蓋 `train` 的計算）；k3s e2e。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 底線開頭的函式被跨檔 import | 拆檔時 | 讀的人會以為是私有的 | 下次碰到那個檔時去掉底線 |
| `cmd_baseline.py` 還有 349 行 | 拆完 | 共用輔助多，再拆要互相 import | 夠用；收斂另開 `cmd_alert.py`，不往這裡加 |
| 舊的 PDCA 紀錄寫「改了 `__main__.py`」 | 搜文件 | 那段程式現在在 `cmd_*.py` | 不改歷史紀錄 |

- 跨檔 import 的底線函式：`_now`、`_v`、`_freq`、`_metric_frame`。
- 這次不改名，差異量才看得出「只是搬」。
- **決定**：一個指令（組）一個檔；共用的放 `common.py`、`tuning.py`。
- 之後告警出口放 `cmd_alert.py`，RAG 放 `engine/core/`。

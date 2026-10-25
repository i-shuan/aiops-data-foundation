# detector 的指令進入點拆檔（2026-10）

**狀態**：進行中　　**日期**：2026-10-25　　**相關**：分支 `claude/split-detector-cli`
**模組**：④⑤⑨（只是整理程式，不改行為）

## Plan：這一次要達到什麼

- **需求**：`engine/detector/__main__.py` 有 1,100 行，13 個指令（train、detect、tune、adjust、rules、peer、quality、chronic、feedback、incident、trace-*）的邏輯全在同一個檔。接下來告警出口、回饋入口、RAG 都要往這裡加，再加就更難讀、改一處怕弄壞別處。
- **目的**：一個指令（或一組相關指令）一個檔，`__main__.py` 只剩參數解析和分派；**行為一字不改**：單元測試照過、`python3 -m detector <指令>` 的用法、輸出、寫進 ES 的東西都一樣。
- **範圍**：只搬程式碼、補 import；不改函式內容、不改名（連 `_now` 這類底線開頭的也先不改，減少這次的差異量）。不碰 `investigator/`。

| 新檔（`engine/detector/`） | 放什麼 | 從哪來 |
|---|---|---|
| `common.py` | 大家共用的小工具：`keys_of`、`_freq`、`_now`、`_v`、`RULE_FIELDS`（規則、peer、quality、trace-detect 寫異常總表時補的欄位） | `__main__.py` 開頭 |
| `tuning.py` | `aiops-tuning` 的 index、mapping、`load_adjustments`／`load_overrides`／`data_adjustments` | baseline、adjust、feedback、trace 四邊都用 |
| `cmd_baseline.py` | train、detect、tune 和它們的輔助（存 baseline、存異常、選粒度、借 baseline、status） | 最大的一塊 |
| `cmd_adjust.py` | adjust | |
| `cmd_feedback.py` | feedback、incident | |
| `cmd_chronic.py` | chronic | |
| `cmd_trace.py` | trace-collect／train／detect | 用 `cmd_baseline` 的存 baseline、存異常 |
| `cmd_rules.py`、`cmd_peer.py`、`cmd_quality.py` | 各一個 | |
| `__main__.py` | 說明、argparse、分派表 | 剩下的 |

## Do：改動哪些 code、改了什麼、為何要改

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）

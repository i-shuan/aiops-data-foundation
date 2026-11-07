# 欄位對照的覆寫擴到 trace：歸屬欄位、raw 索引（2026-11）

**狀態**：Plan　　**日期**：2026-11-07　　**相關**：分支 `claude/mapping-trace`（從 `claude/mapping-ui` 開，那條還沒合回 master）；第一版 [2026-11-mapping-ui.md](2026-11-mapping-ui.md)
**模組**：②（讀進來的欄位對照）

## Plan：這一次要達到什麼

- **需求**：使用者問「trace 上面也有支援 mapper 嗎」。第一版只開放歸屬與 log；trace 的兩件事還是要改程式或設定檔：
  1. APM 的 span 讀部門、群組是寫死的 `labels.dept`、`labels.group`（`data/source/apm.py`），在 `/mapping` 核准把部門改到別的欄位，metrics 與 log 的異常跟著改，**trace 合成時還是讀舊的**（`traces-merged-<group>` 的分法、文件的 `dept` 會不一致）。
  2. raw trace 讀哪個索引（`trace.yaml` 的 `raw_index`）要改設定檔再部署。
- **目的**：①trace 合成時，部門、群組用跟歸屬同一份（含覆寫）的欄位名；②raw 索引也能網頁提、命令列核准，核准前先對真的資料試（索引有沒有資料、格式是不是宣告的來源）。
- **範圍**：只做這兩件。**不做**：trace id、耗時、上一層 span 這些結構欄位（決定 trace 接不接得起來，寫錯整段壞；新格式寫新的來源適配器）；序列（`series.yaml`）。
- **假設**：同一個來源的 agent 在 metrics 與 trace 用同一個 label 路徑（APM 都是 `labels.dept`；OTel 都是 `resource.attributes.dept`），所以 trace 直接用歸屬的 `dept`、`group` 欄位名。

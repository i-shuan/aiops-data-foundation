# retention 三份只留共用部分，其餘移到 log、metrics、trace（2026-10）

**狀態**：完成　　**日期**：2026-10-31　　**相關**：分支 `claude/retention-split`（接在 `claude/log-readme`、`claude/metrics-trace-readme` 之後，兩者尚未合入 master）
**模組**：③ 存

## Plan：這一次要達到什麼

- **需求**：`retention.md`、`retention-reference.md`、`retention-how-to.md` 把 log、metrics、trace 各自的保留依據、每筆大小、怎麼套用、實測、降量做法混在一起；各資料的人要翻三份文件才湊得齊。
- **目的**：跟某一種資料有關的，移進該資料的 README；三者共用的留在 retention 三份；retention 只留共用的。
- **範圍**：只動這三份加 `log`、`metrics`、`trace` 三份 README。

## Do：改動哪些 code、改了什麼、為何要改

| 內容 | 去哪 |
|---|---|
| metrics：為什麼降解析度、baseline 的 `fine_history`、400 天與 90 天、每筆大小（Pod 資源 60、K8s 物件 50、應用 80 bytes）、`metrics-otel@custom` 與 `metrics-hist-otel` 怎麼套用、直方圖降解析度與 ILM 降解析度實測、FDC 感測器不放這套 ES | `docs/metrics/README.md`「多久壓縮」底下 |
| log：為什麼留統計、稽核 log 另算、每筆大小（250 bytes）、量太大時怎麼降、EAP 容量與公式、`aiops-log-stats` 實測 | `docs/log/README.md`「多久壓縮」底下 |
| trace：為什麼留最短、每筆大小（400 bytes）、`traces-otel@custom` 與 `traces-problem` 模板怎麼套用、調低 `RANDOM_PCT` 與不用 `spanmetrics`、三項實測 | `docs/trace/README.md`「多久壓縮」底下 |
| 共用（留下）：原則三條、三種資料留多久的差別表、容量估算（公式、低流量、高流量、依服務數、混合例子）、待決定；保留一覽、failure store、`aiops-*`、rollover 與副本的說明；天數怎麼套用、舊政策還在跑、ILM 照時間刪除與重跑腳本的實測、怎麼量正式環境的量 | `retention.md`、`retention-reference.md`、`retention-how-to.md` |
| 刪除 | 各資料的細項（已移走）；「log 像日記，trace 像錄影帶」的長篇對比（要點移進 trace）；`docs/README.md` 三列說明更新 |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 沒有斷連結（含 retention 連到各 README 的錨點）、索引完整 | `python3 tools/check-docs.py` | 四項都是 0 個問題 |
| 移走的內容沒有漏 | 逐段對照三份舊檔（git 歷史）：每個小節與表格列都有去處 | 見 Do 的表；沒有「兩邊都沒留」的項目 |

- **沒測到的**：移進各 README 的內容照原文搬，沒有重新對程式核對（例如每筆大小、實測數字）。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 保留一覽表（`retention-reference.md`）與各 README 的「多久壓縮」表是同一批天數寫兩處 | 拆分時 | 改天數要改兩處 | 天數都來自 `retention.conf`；要去重可把一覽改成只列 ILM 名稱與連結 |
| 混合例子（EAP 100GB／天）依賴 `log/README.md` 的 EAP 公式 | 拆分時 | 改一邊要記得改另一邊 | 無 |

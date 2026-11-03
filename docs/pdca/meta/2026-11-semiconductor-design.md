# 半導體（FDC）設計文件（2026-11）

**狀態**：已完成（文件）　　**日期**：2026-11-03　　**相關**：分支 `claude/semiconductor-design`；成果 [semiconductor-design.md](../../semiconductor-design.md)
**模組**：④⑥⑦

## Plan：這一次要達到什麼

- **需求**：使用者問半導體 FDC 沒有 trace id 時，RCA、事件整合、RCA graph 怎麼做、跟目前的設計差在哪。
- **目的**：一份獨立的設計文件，用表格對比目前設計，讓拿到 MES、FDC 樣本後能照著開工。
- **範圍**：只寫文件；不寫路徑合成器、不改程式、不建圖。

## Do：改動哪些 code、改了什麼、為何要改

無 code 改動。新增 [docs/semiconductor-design.md](../../semiconductor-design.md)（核心想法、①～⑨ 與五個模型的對照表、事件整合與 graph、資料差別、做的順序、風險），並更新 `docs/README.md`、`docs/pdca/README.md`。

## Check：測試的目的與結果

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 文件沒有斷連結、索引完整、類型標了 | `tools/check-docs.py` | 0 個問題 |
| 對照表裡「目前」那欄跟程式一致 | 對 `detector/model.py`、`investigator/alerts.py`、`analyze.commonality`、README 的業務欄位 | 一致；半導體那欄是設計推論，**未驗證** |

## Act：Check 後沒改善的、還沒解決的

| 沒解決的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 欄位名、分組鍵、分類表都是假設 | 沒有 MES 歷程、FDC run summary 的樣本 | 開工時要重對 | 拿到樣本後先做步驟 1（只做 M1、M3，不需要路徑） |
| lot 路徑會分叉、回頭、合批，span 是樹 | 設計時 | 路徑合成器要處理 rework、split／merge | 看樣本裡這類 lot 的比例再決定 |

- **決定**：先做不需要路徑的 M1、M3，路徑合成器放第 3 步；RCA graph 先用 pandas 自連接，不上 graph DB。

# 量化：學之前、學之後的數字（2026-10）

**狀態**：進行中　　**日期**：2026-10-25　　**相關**：分支 `claude/loop-complete`；主線 [product-loop](2026-10-product-loop.md) 順序 7
**模組**：⑨ feedback

## Plan：這一次要達到什麼

- **需求**：賣「會學」一定要有數字。現在 `feedback summary` 只算誤報率，沒有「從告警到有人回饋多久」「每天幾件」「依部門」，也沒辦法比「某個時間點之前 vs 之後」。
- **目的**：一個指令 `detector metrics --from … --to … [--compare-from … --compare-to …]`，印出：每天幾件（依 dept）、有回饋的比例、誤報率、根因錯的比例、漏報數、從告警開始到第一筆回饋的中位時間、「怎麼看出來的」有填的比例（經驗有沒有留下來）。兩段時間並排就是「學之前 vs 學之後」。
- **範圍**：只算、只印（Markdown）；不畫圖（Grafana 讀 `aiops-alerts`、`aiops-feedback` 自己畫）。資料從試點產線來，這裡先用模擬的驗算法。

## Do：改動哪些 code、改了什麼、為何要改

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）

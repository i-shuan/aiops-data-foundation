# 動態基準線的誤報：p95 跳桶、量少的錯誤率（2026-10）

**狀態**：進行中（Plan 完成，等 ES 名額執行 Do／Check）　　**日期**：2026-10-28　　**相關**：[baseline-verify](2026-10-baseline-verify.md) 的 Act
**模組**：④ 判斷

## Plan：這一次要達到什麼

- **需求**：[baseline-verify](2026-10-baseline-verify.md) 用沒有異常的一週合成資料量誤報率：anomaly 1.50%、warning 1.00%，目標是 ≤ 0.1%、≤ 1%。
- **目的**：只改設定，先把誤報降到目標內，真的異常照樣抓到。根本修法（方案 D）另一輪做。
- **範圍**：`engine/configs/baseline/series.yaml`，不改程式。不處理 `hour` 層平日、週末混在一起，也不處理尖峰不對齊整點（baseline-verify 的 Act 另列）。

### 誤報從哪來

| 序列 | anomaly 率 | 原因 |
|---|---|---|
| `http.latency_p95_s` | 3.52%（佔全部 88%） | p95 從直方圖估，值是桶的中點（0.175、0.375）；平常幾乎不動 → MAD＝0、σ 只剩平常值的 5%；跨一格 z 約 23，超過 `instant_z: 15` 直接 anomaly |
| `http.error_rate` | 0.34% | 每窗至少 20 個請求就判斷（`min_events.rate: 20`）；20 個裡 1 個 5xx 就是 5%，等於 `min_abs`，加上 `confirm_points: 1` 一次就 anomaly |

### 這一輪做的（A＋E）

| 方案 | 改什麼 | 預期效果 | 代價 |
|---|---|---|---|
| A | `http.latency_p95_s` 加 `instant_z: null`（不走「偏太多就直接 anomaly」） | 跨一格只算 warning，連續 2 個時間窗都高才 anomaly | 真的突然變慢，晚一個時間窗才 anomaly |
| E | `adaptive.min_events.rate` 20 → 100 | 1 個 5xx 最多 1%，到不了 `min_abs` 5%；p95 每窗請求多，比較穩 | 全域生效：所有比例、百分位序列（錯誤率、p95）都要 ≥ 3 成時間窗有 100 個請求，量少的 API 會選更粗的粒度或不判斷 |

- `instant_z` 要寫 `null`，不能寫 0：判斷是 `|z| ≥ instant_z`，0 會讓每個超標點都直接 anomaly。
- 不選 B（`min_abs` 0.05 → 0.25）：真的慢 200 毫秒會漏。不選 C（p95 固定 5 分鐘）：偵測慢 5 分鐘。

### 下一輪（D，改程式）

值從直方圖算出來時，σ 下限改成半個桶寬（桶邊界從資料取），取代「平常值的 5%」。跨一格的 z 從約 23 降到約 2，跨很多格才報。A＋E 的結果出來後再開。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/configs/baseline/series.yaml` | `http.latency_p95_s` 加 `instant_z: null` | 方案 A |
| 同上 | `adaptive.min_events.rate: 100` | 方案 E |
| `engine/configs/baseline/README.md` | 限制表更新 | 實測後的結論 |

## Check：測試的目的與結果

用 baseline-verify 的同一份資料和程式重跑，換掉的只有設定：`ES_URL=… python -m tests.baseline_edge_e2e --fresh`（約 6 分鐘，要拿 ES 名額）。結果不合時不放寬預期，寫進 Act。

| 檢查 | 改之前 | 預期 | 實際 |
|---|---|---|---|
| 一週 anomaly 率（合計） | 1.50% | ≤ 0.1% | 待測 |
| 一週 warning 率（合計） | 1.00% | ≤ 1% | 待測 |
| `http.latency_p95_s` anomaly 率 | 3.52% | 大幅下降 | 待測 |
| `http.error_rate` anomaly 率 | 0.34% | 大幅下降 | 待測 |
| 剛上線各層：延遲 5 倍 10 分鐘 | anomaly | anomaly | 待測 |
| MAD＝0：延遲換一格、持續 5 分鐘 | anomaly | anomaly（連續 2 窗） | 待測 |
| 錯誤率單點 50% | anomaly | anomaly（E 可能讓量少的時間窗不判斷，要看） | 待測 |
| 單點尖峰 z ≥ 15（請求數，不受 A 影響） | anomaly | anomaly | 待測 |
| 單元測試 | 324 passed | 全過 | 待測 |

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 跨一格的 z 仍然很大，只是不直接 anomaly | 方案 A 的設計 | 連續兩窗都卡在邊界時仍報 anomaly | 下一輪方案 D |

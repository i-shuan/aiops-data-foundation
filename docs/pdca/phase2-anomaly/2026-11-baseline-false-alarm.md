# 動態基準線的誤報第三輪：p95 半個桶寬、錯誤率至少 N 個 5xx（2026-11）

**狀態**：已完成　　**日期**：2026-11-01　　**相關**：分支 `claude/false-alarm-tune`；前一輪 [baseline-threshold-tune](2026-10-baseline-threshold-tune.md)、[open-issues 9.2](../meta/2026-10-open-issues.md)
**模組**：④ 判斷（動態基準線學習）

## Plan：這一次要達到什麼

- **需求**：前一輪只留方案 A（p95 不走單點即判定），一週沒有異常的合成資料仍有 anomaly 0.53%（目標 ≤ 0.1%）、warning 1.97%（目標 ≤ 1%）。來源是兩個：
  - `http.latency_p95_s`：p95 從直方圖算，值落在桶的中點；平常幾乎不動，MAD 是 0，σ 只剩中位數的 5%，跨一格 z 約 23。
  - `http.error_rate`：20～40 個請求裡 1 個 5xx 就到 `min_abs` 5%，加上 `confirm_points: 1`，一週 205 個 anomaly。
- **目的**：同一份資料與 e2e（`tests.baseline_edge_e2e`）重量，anomaly、warning 都比前一輪少；注入的真異常照樣抓到。
- **範圍**：
  - 做：方案 D——p95 的 σ 下限加上「中位數所在那一格的半個桶寬」，桶的邊界用 OTel 語意慣例的預設值；錯誤率加「這個時間窗至少 N 個 5xx 才算異常」。
  - 不做：requests 尖峰不對齊整點（baseline-verify 的 Act 另列）；`hour` 層平日週末混在一起；改 `min_events`（前一輪試過，副作用太大）。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `detector/model.py` | 新增 `bucket_floor`：中位數所在那一格寬度的一半（超過最後一個邊界用最後一格）；`score` 多一個 `buckets` 參數，有給時 σ 的下限多這一項 | p95 平常落在同一格、MAD 是 0，σ 只剩中位數的 5%，跨一格 z 約 23。半個桶寬時跨一格 z 約 2～3 |
| `detector/cmd_baseline.py` | `score_tuned`、`detect_borrowed` 把序列的 `buckets` 傳給 `score`；新增 `apply_min_count`：值 × 事件數（5xx 個數）不到 `min_count` 的點 `anomaly` 改成 False，`detect_series`、`detect_borrowed` 都套 | 20 個請求錯 1 個就是 5%，等於 `min_abs`，錯誤率又是單點即判定 |
| `configs/baseline/series.yaml` | `http.latency_p95_s` 加 `buckets`（OTel 預設邊界，秒）；`http.error_rate` 加 `min_count: 3` | 3 是估的 |
| `tests/test_model.py`、`tests/test_min_count.py`（新） | 單元測試 | |
| `tests/baseline_edge_e2e.py` | 「階梯式改變（只跨一格）」與「reset_point proposed」兩個預期從「整天報（≥ 2000 點）」改成「不再整天報（< 100 點）」 | 2 倍 = p95 跨一格，現在是 warning 等級；原預期記的是 p95 跳桶誤報的一種 |

第一版的桶寬是從「這條序列學習期間出現過的值」算的，e2e 證明不行：ES 在桶之間內插出 0.185 這種值，學出來的桶寬只有 0.01，下限等於沒加（p95 誤報的 z 仍是 22.9）。改成直接用直方圖的邊界。

## Check：測試的目的與結果

環境：Rancher Desktop、ES 8.19.23；`tests.baseline_edge_e2e --fresh`，跟前一輪同一份合成資料（一週 162,510 個點、沒有注入異常的服務；10-19～10-25 每天 train、隔天 detect）。

| 為了確認什麼 | 怎麼測 | 結果 |
|---|---|---|
| 一週誤報率有沒有降到目標（anomaly ≤ 0.1%、warning ≤ 1%） | 同上，各版本各跑一次 | 前一輪：anomaly 0.530%（861）、warning 1.97%。第一版（桶寬從學習期間的值算）：0.479%（778）、2.151%，**沒降**。最終版（OTel 桶邊界＋`min_count: 3`）：**anomaly 0.068%（110）、warning 0.008%（13）**，兩項都達標；不含 svc-peak 是 anomaly 0.006%（8）、warning 1 |
| p95 誤報是不是消了 | 同一份表，各序列 warning／anomaly | svc-peak 325／8 → 0／0；svc-season 360／8 → 0／0；svc-spike 2,798／652 → 0／0 |
| 錯誤率「至少 3 個 5xx」 | 同一份表 | 錯誤率 anomaly 205（前一輪，60,595 點）→ 18（svc-peak 10、svc-season 8；svc-flat、svc-spike 0） |
| 真異常還抓不抓得到、有沒有新的失敗 | 新舊兩份 e2e 結果逐項比對 | 舊版 PASS 的案例只有兩個變成 FAIL：「階梯式改變（只跨一格）」與「reset_point proposed」，兩個預期的是 p95 跳桶誤報，已改預期。「MAD＝0：延遲換一格」、svc-spike 的注入異常、重新 train 後的 detect 都照樣過；另外「緩慢漂移」「svc-spike 其他時間」「重新 train 後 detect」三項從 FAIL 變 PASS |
| 單元測試 | `pytest` | 419 個通過；新增 `bucket_floor`（超過最後一個邊界、NaN）、跨一格不報、跨多格仍報、`apply_min_count`（1、2 個不算，3 個以上算，浮點取整） |

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 剩下的 110 個 anomaly 裡 92 個是 `http.requests` svc-peak（尖峰不對齊整點） | 誤報表 | 這輪沒處理（範圍外） | baseline-verify 的 Act：邊界比相鄰時段，或 `recurring_override` |
| **p95 只跨一格的變慢不再是 anomaly** | 「階梯式改變」案例：2 倍（0.175 → 0.375 秒）以前整天報，現在 z 約 2.7，最多 warning | 平常在同一格的 API，延遲變 2 倍只靠靜態規則（p95 ≥ 2 秒）抓，baseline 這一路要跨好幾格才報。這是用靈敏度換誤報，沒有量「真的變慢漏報多少」 | 要抓這種得調小 `buckets` 或個別 `tune --z`；上線後看漏報回饋再決定 |
| `buckets` 假設應用用 OTel 預設邊界 | 寫設定時；e2e 的合成資料用的就是這組 | 應用改過 bucket 邊界時，半個桶寬算錯（太寬漏報、太窄誤報） | `series.yaml` 的 `buckets` 跟著改；邊界能從資料讀到時再自動化 |
| `min_count: 3` 是估的 | 設計時 | 量很低的 API（每窗 20 個請求），3 個 5xx 就是 15%，仍報；每窗 100 個請求時 3 個只有 3% 但也報（`min_abs` 5% 擋住） | 上線後看 |
| e2e 剩 7 個 FAIL（流量掉到一成、尖峰不對齊整點、剛上線 3d、稀疏、收集中斷 15:40、`resolution=5m` 等） | 這輪前後都有，不是這輪造成 | 沒處理 | 見 baseline-verify、baseline-threshold-tune 的 Act |

# 動態基準線的誤報：p95 跳桶、量少的錯誤率（2026-10）

**狀態**：已完成兩輪，Check 有沒過的項目（只留 A：anomaly 1.50% → 0.53%、warning 1.00% → 1.97%，都沒到目標；真異常都抓到；下一輪改程式：D、錯誤率至少 N 個 5xx）　　**日期**：2026-10-29　　**相關**：[baseline-verify](2026-10-baseline-verify.md) 的 Act
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

### 第一輪做的（A＋E）

| 方案 | 改什麼 | 預期效果 | 代價 |
|---|---|---|---|
| A | `http.latency_p95_s` 加 `instant_z: null`（不走「偏太多就直接 anomaly」） | 跨一格只算 warning，連續 2 個時間窗都高才 anomaly | 真的突然變慢，晚一個時間窗才 anomaly |
| E | `adaptive.min_events.rate` 20 → 100 | 1 個 5xx 最多 1%，到不了 `min_abs` 5%；p95 每窗請求多，比較穩 | 全域生效：所有比例、百分位序列（錯誤率、p95）都要 ≥ 3 成時間窗有 100 個請求，量少的 API 會選更粗的粒度或不判斷 |

- `instant_z` 要寫 `null`，不能寫 0：判斷是 `|z| ≥ instant_z`，0 會讓每個超標點都直接 anomaly。
- 不選 B（`min_abs` 0.05 → 0.25）：真的慢 200 毫秒會漏。不選 C（p95 固定 5 分鐘）：偵測慢 5 分鐘。

### 下一輪（D，改程式）

值從直方圖算出來時，σ 下限改成半個桶寬（桶邊界從資料取），取代「平常值的 5%」。跨一格的 z 從約 23 降到約 2，跨很多格才報。連同錯誤率「至少 N 個 5xx 才判斷」一起，第二輪之後開。

### 第二輪：使用者決定退回 E、只留 A

E 讓一般 API（每 30 秒 40 個請求）的錯誤率、p95 從 30 秒一點變成 5 分鐘一點：30 秒的錯誤率尖峰被稀釋、5 分鐘的變慢只剩 warning。使用者決定退回 `min_events.rate` 20，只留 A，同一份 e2e 再量一次，量出 A 單獨的效果。「至少 N 個 5xx」和 D 都要改程式，這輪不做。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/configs/baseline/series.yaml` | `http.latency_p95_s` 加 `instant_z: null` | 方案 A。`status_params` 取序列自己的鍵時 `null` 不會退回全域 15，`mark_status` 收到 `None` 就不走「直接 anomaly」 |
| 同上 | `adaptive.min_events.rate` 20 → 100 | 方案 E（第一輪） |
| 同上 | `adaptive.min_events.rate` 100 → 20 | 第二輪退回 E，只留 A |
| `engine/configs/baseline/reference.md`（拆檔前是 README.md） | 粒度門檻表、限制表 | 實測結論 |
| `docs/troubleshooting.md` | p95 跳桶那一列 | 做法變了 |

`tests/baseline_edge_e2e.py` 執行時讀 `series.yaml`（`make_config`），測試沒改。

## Check：測試的目的與結果

### 第一輪（A＋E）

用 baseline-verify 的同一份資料和程式重跑，換掉的只有設定：`ES_URL=… python -m tests.baseline_edge_e2e --fresh`（自己的 ES 9.5.3；59 PASS、21 FAIL，320 秒）。結果不合時不放寬預期，寫進 Act。

**E 的副作用**：每 30 秒 40 個請求（晚上 8 個）的 API，比例、百分位類也湊不到「三成時間窗 ≥ 100 個請求」，錯誤率、p95 全部從 30 秒一點變成 5 分鐘一點（requests、log 不受影響）。改之前、改之後的點數因此不能直接比，下表另附「報 anomaly 的總時間」。

| 檢查 | 改之前 | 預期 | 實際 |
|---|---|---|---|
| 一週 anomaly 率（合計） | 1.50%（2,430／162,510） | ≤ 0.1% | ❌ 0.172%（93／54,008）；92 個是 requests 尖峰不對齊整點（不在這輪範圍），不含 svc-peak 0.002%（1／41,667） |
| 一週 warning 率（合計） | 1.00%（1,629／162,510） | ≤ 1% | ✅ 0.122%（66／54,008） |
| `http.latency_p95_s` anomaly 率 | 3.52%（2,133／60,595，30 秒；共約 17.8 小時） | 大幅下降 | ✅ 0.016%（1／6,344，5 分鐘；共 5 分鐘）；warning 0.84%（53），其中 52 個是漂移的 svc-spike |
| `http.error_rate` anomaly 率 | 0.34%（205／60,595，30 秒；共約 1.7 小時） | 大幅下降 | ✅ 0%（0／6,344，5 分鐘）；warning 0 |
| `http.requests` | 0.23%（92／40,483），warning 13 | 不變 | ✅ 不變：0.23%（92／40,483），warning 13 |
| `log.lines`、`log.errors` | 0%（0／837） | 不變 | ✅ 0%（0／837） |
| 剛上線各層：延遲 5 倍 10 分鐘 | 四層各 20 個 anomaly（30 秒） | anomaly | ✅ 四層各 1 個 anomaly（5 分鐘一點） |
| MAD＝0：延遲換一格、持續 5 分鐘 | anomaly | anomaly（連續 2 窗） | ❌ 只有 1 個 5 分鐘點，warning |
| 錯誤率單點 50%（30 秒） | anomaly | anomaly | ✅ anomaly（5 分鐘平均 5.46%，剛過 `min_abs` 5%） |
| 錯誤率 33%（30 秒，30 個請求錯 10 個） | anomaly | anomaly | ❌ 不報：5 分鐘平均 3.3%，不到 `min_abs` 5% |
| 單點尖峰 z ≥ 15（請求數，不受 A 影響） | anomaly | anomaly | ✅ anomaly（z 33.4） |
| 單元測試 | 324 passed | 全過 | ✅ 336 passed |

### e2e 其他檢查的變化（標準沒改）

| 檢查 | 改之前 | 改之後 | 原因 |
|---|---|---|---|
| 調整介面對照組：svc-ta／tb 延遲 3 倍 5 分鐘 | anomaly | ❌ warning | 一個 5 分鐘點、不再直接 anomaly；拿它當對照的 6 項跟著 ❌（exclude_window 三項、`recurring_override` mute、壞調整 detect、對照本身） |
| `reset_point` 後重新 train：16:30 變慢 3 倍 5 分鐘 | anomaly（另 warning 67） | ❌ warning（其他 0） | 同上；p95 跳桶的 warning 沒了 |
| override `resolution=5m` | train 前 30 秒、後 ta 5m、tb 30 秒 | ❌ train 前就 5m、tb 也 5m | 自動選的粒度已經是 5m，這項量不出差別 |
| 階梯式改變沒 `reset_point`；`reset_point` proposed | 2,718 個 anomaly | ❌ 273 個 | 檢查用 30 秒點數算（≥ 2,000）；273 個 5 分鐘點＝整天都報，行為沒變 |
| 緩慢漂移（svc-spike 延遲每天 +1%）；svc-spike 其他時間 | anomaly 345 | ❌ anomaly 0、warning 11 | 預期 0 個異常，仍有 p95 的 warning |
| 收集中斷，正式環境的 detect（16:10） | 16:00 後 23 個誤報 | ❌ warning 1（p95） | 中斷時段內 0 |

其餘 baseline-verify 已知沒過的照舊：白天流量掉到一成、尖峰不對齊整點、剛上線 3 天 warning 2、稀疏 10 點、15:40 的 detect 報 svc-rare 14 點的真異常。

### 第二輪（只留 A）

同一份資料、程式，只把 `min_events.rate` 退回 20：68 PASS、12 FAIL（351 秒）。錯誤率、p95 回到 30 秒一點，點數跟改之前可以直接比。

| 檢查 | 改之前 | 第一輪 A＋E | 第二輪 只留 A | 預期 |
|---|---|---|---|---|
| 一週 anomaly 率（合計） | 1.50%（2,430／162,510） | 0.172%（93／54,008） | ❌ 0.530%（861／162,510）；不含 svc-peak 0.499%（662／132,723） | ≤ 0.1% |
| 一週 warning 率（合計） | 1.00%（1,629／162,510） | 0.122%（66／54,008） | ❌ 1.968%（3,198／162,510） | ≤ 1% |
| `http.latency_p95_s`（60,595 點，30 秒） | anomaly 3.52%（2,133；共約 17.8 小時）、warning 1,616 | anomaly 0.016%（1／6,344，5 分鐘）、warning 53 | ❌ anomaly 0.93%（564；共約 4.7 小時）、warning 5.26%（3,185）：超標的點數沒變（3,749），只是從 anomaly 改成 warning；漂移的 svc-spike anomaly 553、warning 2,602，其他三個服務 anomaly 11、warning 583 | 大幅下降 |
| `http.error_rate`（60,595 點，30 秒） | anomaly 0.34%（205；共約 1.7 小時） | 0%（0／6,344，5 分鐘） | ❌ 0.34%（205）：回到改之前，A 不碰錯誤率 | 大幅下降 |
| `http.requests` | 0.23%（92／40,483），warning 13 | 不變 | ✅ 不變 | 不變 |
| `log.lines`、`log.errors` | 0%（0／837） | 0% | ✅ 0% | 不變 |
| 剛上線各層：延遲 5 倍 10 分鐘 | 四層各 20 個 anomaly | 四層各 1 個（5 分鐘一點） | ✅ 四層各 19 個 anomaly（第 1 點 warning） | anomaly |
| MAD＝0：延遲換一格、持續 5 分鐘 | anomaly | ❌ warning（1 個 5 分鐘點） | ✅ 10 點：第 1 點 warning、之後 anomaly | anomaly（連續 2 窗） |
| 錯誤率單點 50%（30 秒） | anomaly | anomaly | ✅ anomaly | anomaly |
| 錯誤率 33%（30 秒，30 個請求錯 10 個） | anomaly | ❌ 不報（5 分鐘平均 3.3%） | ✅ anomaly | anomaly |
| 單點尖峰 z ≥ 15（請求數） | anomaly | anomaly（z 33.4） | ✅ anomaly（z 33.4） | anomaly |
| 調整介面對照組：svc-ta／tb 延遲 3 倍 5 分鐘 | anomaly | ❌ warning（連帶 6 項 ❌） | ✅ warning → anomaly（連帶 6 項 ✅） | anomaly |
| override `resolution=5m`：ta 同一個 5 分鐘的變慢 | anomaly | ❌ 量不出差別（自動已是 5m） | ❌ 粒度 30s → 5m 對，但 10:00 只有 warning：5 分鐘的變慢在 5m 粒度只有 1 點，A 之下到不了連續 2 窗 | anomaly |
| `reset_point` 後重新 train：16:30 變慢 3 倍 5 分鐘 | anomaly，另 warning 67 | ❌ warning，其他 0 | ❌ anomaly，另 warning 67（p95 跳桶，同改之前） | 只報 16:30 |
| 階梯式改變、`reset_point` proposed | 2,718 個 anomaly | ❌ 273 個 5 分鐘點 | ✅ 2,675 個 | 整天報 |
| 緩慢漂移（svc-spike 延遲每天 +1%）；svc-spike 其他時間 | anomaly 345 | anomaly 0、warning 11 | ❌ anomaly 89、warning 431 | 0 個異常 |
| 收集中斷，正式環境的 detect（16:10） | 23 個誤報 | warning 1 | ❌ anomaly 20、warning 6（16:00 那幾點的 p95、錯誤率） | 0 個異常 |
| e2e | 69 PASS、11 FAIL | 59 PASS、21 FAIL | 68 PASS、12 FAIL：比改之前多 1 個（`resolution=5m`） | — |
| 單元測試 | 324 passed | 336 passed | ✅ 346 passed | 全過 |

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| A 單獨：p95 超標的點沒少，只是從 anomaly 變 warning | 第二輪：anomaly 2,133 → 564、warning 1,616 → 3,185，合計 3,749 → 3,749 | 合計 warning 1.97% 超過 1%、anomaly 0.53% 超過 0.1% | 下一輪方案 D（σ 下限＝半個桶寬，改程式）：跨一格 z 約 23 → 約 2，超標的點才會真的少 |
| 跨一格的 z 仍然很大，只是不直接 anomaly | 方案 A 的設計 | 連續兩窗都卡在邊界就 anomaly；漂移的 svc-spike 一週 anomaly 553、warning 2,602 | 同上，方案 D |
| 粒度 ≥ 事件長度時，A 讓它永遠只有 warning | override `resolution=5m` 的 5 分鐘變慢只有 1 點（第一輪 E 把一般 API 推到 5m 也是同一件事） | 設 5m 的序列抓不到 5 分鐘內的變慢 | D 做完後再評估要不要恢復 p95 的 `instant_z` |
| 錯誤率 1 個 5xx 就到門檻 | E 已退回（第二輪），回到改之前：一週 205 個（0.34%） | 20～40 個請求裡 1～2 個 5xx 就 anomaly | 下一輪改程式：至少 N 個 5xx 才判斷 |
| 合計 anomaly 0.53% 仍超過 0.1% | p95 564、錯誤率 205、requests 尖峰不對齊整點 92 | 一週 861 個誤報 | p95 → D；錯誤率 → N 個 5xx；requests → baseline-verify 的 Act（邊界比相鄰時段，或 `recurring_override`） |

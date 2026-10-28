# 動態基準線的誤報：p95 跳桶、量少的錯誤率（2026-10）

**狀態**：已完成，Check 有沒過的項目（誤報降了，但 5 分鐘內的變慢、30 秒的錯誤率尖峰抓不到；待決定）　　**日期**：2026-10-29　　**相關**：[baseline-verify](2026-10-baseline-verify.md) 的 Act
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
| `engine/configs/baseline/series.yaml` | `http.latency_p95_s` 加 `instant_z: null` | 方案 A。`status_params` 取序列自己的鍵時 `null` 不會退回全域 15，`mark_status` 收到 `None` 就不走「直接 anomaly」 |
| 同上 | `adaptive.min_events.rate` 20 → 100 | 方案 E |
| `engine/configs/baseline/README.md` | 粒度門檻表、限制表 | 實測結論 |
| `docs/troubleshooting.md` | p95 跳桶那一列 | 做法變了 |

`tests/baseline_edge_e2e.py` 執行時讀 `series.yaml`（`make_config`），測試沒改。

## Check：測試的目的與結果

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

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| E 讓一般 API 的錯誤率、p95 變 5 分鐘一點 | 每 30 秒 40 個請求也湊不到三成時間窗 ≥ 100 | 持續 5 分鐘的變慢只有 warning；30 秒的錯誤率尖峰被 5 分鐘平均稀釋（33% → 3.3% 不報）；anomaly 最快 10 分鐘才出 | 待決定：退回 E、只留 A 再量一次；或錯誤率改成「至少 N 個 5xx」（要改程式） |
| A、E 一起改，分不出各自降了多少 | 這輪的設計 | 不知道只留 A 夠不夠 | 只套 A 重跑同一份 e2e |
| 跨一格的 z 仍然很大，只是不直接 anomaly | 方案 A 的設計 | 連續兩窗都卡在邊界時仍報 anomaly | 下一輪方案 D |
| 合計 anomaly 0.172% 仍超過 0.1% | 92 個是 requests 尖峰不對齊整點 | 一週 92 個誤報 | baseline-verify 的 Act（邊界比相鄰時段，或 `recurring_override`） |

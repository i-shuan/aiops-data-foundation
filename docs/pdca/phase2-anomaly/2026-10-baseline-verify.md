# 動態基準線學習：邊緣情境、誤報率、調整介面（2026-10）

**狀態**：已完成　　**日期**：2026-10-28　　**相關**：從 `14a12ed`（phase1 合併版）開的 worktree 分支
**模組**：④ 判斷（② 收集中斷的處理、⑨ learning loop 的調整介面）

## Plan：這一次要達到什麼

- **需求**：動態基準線學習（中位數＋MAD、卜瓦松、退層、粒度、`confirm_points`、`instant_z`）只用整齊的模擬資料驗過；Collector 真正寫出來的形狀（exponential histogram、30 秒 delta）、邊緣資料下抓不抓得到、誤報多少，沒有數字。learning loop 寫進 `aiops-tuning` 的調整，生效時機與優先順序沒實測。
- **目的**：每個邊緣情境寫預期、實測；量出沒有異常的一週的誤報率；確認調整介面「下一輪就生效」、不是 active／過期不生效、衝突誰優先、讀不到不整個失敗。演算法錯就修；門檻問題寫 Act，不改門檻。
- **範圍**：只測 `train`／`detect`（http.\*、log.\*）。不測 trace 每一段的 baseline、Pod 同儕橫向比對、靜態臨界值規則、`backtest`。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/detector/es.py` | 新增 `alive_times`：整個 index、不加 filter 的有資料時間窗 | 「收集系統有沒有在跑」不能用序列自己的 filter 判斷 |
| 同上 | 有 filter 的筆數序列補 0 時用 `alive_times` | 只數 error log 時，沒人出錯的小時被當「不知道」，λ 學成 1.68（實際 1.2） |
| `engine/detector/cmd_baseline.py` | `_add_silent` 只在 `alive_times` 有資料的時間窗補 0 | 收集中斷超過 detect 的 30 分鐘時，每個服務被報成「停掉」 |
| 同上 | `train`、`detect` 用自己的時間點判斷 override 有沒有過期 | 原本 override 看現在、其他調整看 `--at`，回放時兩者不一致 |
| `engine/detector/tuning.py` | `load_overrides(es, cfg, now)` | 同上 |
| `engine/detector/adjust.py` | 讀 `aiops-tuning` 失敗印警告、只用設定檔的；連線錯誤也接住 | 原本連線錯誤整個失敗；index 關閉時默默忽略、沒有提示 |
| 同上 | 新增 `problem()`：缺欄位、正規表示式錯、cron 錯的調整略過並印出 | 原本一筆壞調整讓整個 train／detect 失敗（`KeyError`、`PatternError`） |
| `engine/tests/test_fill.py`、`test_adjust.py` | 補 0 的 alive、`_add_silent`、壞調整、讀不到的單元測試 | — |
| `engine/tests/otlp_synth.py`（新） | 手寫 OTLP protobuf 編碼；同一份文件的 `_bulk` 版與比對 | venv 沒有 protobuf；ES 慢時要能帶 `_id` 重送 |
| `engine/tests/baseline_edge_e2e.py`（新） | 合成資料、四段測試（誤報率、邊緣、reset_point、調整介面），印 PASS／FAIL | 可重跑；寫入中斷可接續 |
| `engine/configs/baseline/README.md` | 限制表、調整的優先順序 | 實測結論 |

## Check：測試的目的與結果

**資料**：自己的 ES 9.5.3。metrics 47.6 萬點（13 個服務，30 秒一點、delta、`service.name` 是 Pod label 的值），log 4.2 萬筆（走 `/_otlp/v1/logs` → `k8s-it-bedge`）。metrics 用 `_bulk` 寫 OTLP 入口寫出來的同一份 `_source`；開頭先比對：6 筆 `_source` 逐欄一樣、p50/p95/p99 一樣、型別 `exponential_histogram`。data stream 改 standard mode（TSDS 的 `look_back_time` 上限 7 天，寫不進 4 週前）。序列定義從 `series.yaml` 複製。指令：`ES_URL=… python -m tests.baseline_edge_e2e --fresh`（69 PASS、11 FAIL，358 秒）。

### 邊緣情境（train 到 10-26 00:00，detect 10-26 一整天）

| 情境 | 預期 | 實際 |
|---|---|---|
| 台灣時區：週一 9 點的平常（每分鐘） | 120（UTC 的話是 80） | ✅ 120 |
| 週末：週六／週一 10 點 | 0.5 倍 | ✅ 40／75 |
| 跨午夜批次：週一 0 點／週日 23 點 | 96／48 | ✅ 95／47 |
| 季節性服務平常的一天（尖峰、跨午夜） | requests 0 個異常 | ✅ 0 |
| 白天流量掉到一成（11:00～11:10） | warning 後 anomaly | ❌ 0 個：10-12 收集中斷使該時段退到 hour，z −4.0 |
| 上班尖峰不對齊整點（08:45 起） | 0 個異常 | ❌ anomaly 14、warning 1（08:45～08:59） |
| 剛上線 1 天／3 天／1 週／2 週：層級 | all／hour／hour／dow_hour | ✅ 同預期（2 週的 10～15 點 hour：收集中斷） |
| 剛上線各層：延遲 5 倍 10 分鐘 | 抓到 | ✅ 四層各 20 個 anomaly |
| 剛上線各層：requests 平常的一天 | 0 個異常 | 1 天、1 週、2 週 ✅ 0；3 天 ❌ warning 2 |
| MAD＝0：requests、p95、錯誤率學出來的 MAD | 0 | ✅ 0 |
| MAD＝0：請求數 +10%／+40% | 不報／報 | ✅ 不報／warning→anomaly |
| MAD＝0：延遲換一格；錯誤率 1/30、33% | 報；不報、anomaly | ✅ 同預期 |
| 稀疏（每 5 分鐘 0～2 筆）用什麼學 | 卜瓦松、1h、λ≈6 | ✅ 卜瓦松、1h、λ 5.80、hour |
| 稀疏：14 點多 24 次 | anomaly | ✅ anomaly |
| 稀疏：其他小時（含 10 點多 6 次） | 0 個異常 | ❌ 10 點 anomaly：亂數抽到 21 次（λ 6.15） |
| 稀疏的延遲、錯誤率（比例類） | 沒有 baseline、不判斷 | ✅ |
| 稀疏 error log（只有一個服務有）：λ | 1.2 | ✅ 1.21（修正前 1.68） |
| 稀疏 error log：20 點多 15 筆 | anomaly | ✅ |
| 收集中斷（歷史 10-12 10～16 點） | 不當 0，週一 12 點約 80、退到 hour | ✅ 74、hour |
| 收集中斷（10-26 15～16 點），detect 一整天 | 中斷時段 0 個異常 | ✅ 0 |
| 收集中斷，正式環境的 detect（15:40、30 分鐘） | 0 個異常 | ✅ 中斷時段 0（修正前 91 個）；另報 1 個 14 點的真異常 |
| 同上（16:10、30 分鐘） | 0 個異常 | 中斷時段 ✅ 0；16:00 後 23 個是 p95、錯誤率的誤報 |
| 緩慢漂移（延遲每天 +1%） | 0 個異常 | ❌ anomaly 345：p95 在桶之間跳（見誤報率） |
| 單點尖峰 z≈8／z 33 | warning／anomaly | ✅ warning／anomaly |
| 連續兩點／隔一點兩點 | warning、anomaly／warning、warning | ✅ 同預期 |
| 錯誤率單點 50% | anomaly | ✅ |
| 階梯式改變（4 天前延遲 2 倍），沒 reset_point | 整天報 | ✅ 2,718 個 30 秒點 |

- 「稀疏 10 點」：預期假設剛好多 6 次（共 12，P≈0.02）；實際抽到 21 次，在 λ 6.15 下 P(X≥21) < 0.001，報是對的，**預期本身錯**。
- 「16:10」：中斷時段內 0；16:00～16:07 的 23 個跟中斷無關（p95 跳桶、一個 5xx），原檢查保留不改。

### 誤報率（10-19～10-26，每天 train、detect 隔天，沒有注入異常的 6 個服務）

| 序列 | 判斷的點 | warning | anomaly | anomaly 率 | 誤報來源 |
|---|---|---|---|---|---|
| `http.requests` | 40,483 | 13 | 92 | 0.23% | 全部是尖峰不對齊整點（08:45～09:00） |
| `http.error_rate` | 60,595 | 0 | 205 | 0.34% | 20～40 個請求裡 1～2 個 5xx 就 ≥ 5% |
| `http.latency_p95_s` | 60,595 | 1,616 | 2,133 | 3.52% | p95 在直方圖桶中點之間跳，MAD＝0 |
| 同上，不含漂移的服務 | 40,441 | 373 | 221 | 0.55% | 同上 |
| `log.lines`、`log.errors` | 837 | 0 | 0 | 0% | — |
| **合計** | **162,510** | **1,629（1.00%）** | **2,430（1.50%）** | **1.50%** | — |

- 預期 anomaly ≤ 0.1%、warning ≤ 1%：**都沒過**。筆數類（requests、log）除了不對齊的尖峰是 0。
- p95：基準是 0.175（0.1～0.25 秒那格的中點）、MAD 0、σ＝5%×0.175；多兩個請求落到下一格，p95 變 0.375，z 22.9，超過 `instant_z` 直接 anomaly。

### learning loop 的調整介面（svc-ta 調、svc-tb 對照；寫入後直接跑下一輪）

| 調整 | 預期 | 實際 |
|---|---|---|
| override `z=50`、`min_abs=1000`（`tune`） | 下一輪 detect ta 不報、tb 照報 | ✅ |
| override `confirm_points=1`、`instant_z=6` | ta 單點就 anomaly、tb warning | ✅（ta z 6.97） |
| override `resolution=5m` | detect 不變；下一輪 train 後 ta 5m | ✅ train 前 30s、後 ta 5m、tb 30s |
| override `status` proposed／rejected | 不生效 | ✅ |
| override `expires_at` 已過／還沒到 | 不生效／生效 | ✅ |
| override 在 `--at` 之後、現在之前過期（回放） | 生效（以 `--at` 判斷） | ✅（修正後） |
| `series.yaml` overrides `z=50` | 生效 | ✅ |
| 衝突：yaml `z=50`、`aiops-tuning` `z=5` | `aiops-tuning` 優先 | ✅ ta 照報 |
| yaml `z=50`、`aiops-tuning` 只設 `confirm_points` | 各自生效 | ✅ ta 不報 |
| `exclude_window` 不報（detect）；proposed；過期 | 不報；不生效；不生效 | ✅ |
| `exclude_window` 不學（排掉兩個週一） | 下一輪 train ta 週一全退到 hour | ✅ tb 不變 |
| `reset_point` proposed → approve | 不生效 → 下一輪 detect 全不報 | ✅ 2,718 → 0（baseline 是重置前學的） |
| `reset_point` 後重新 train | hour 層、只報 16:30 的新異常 | 16:30 ✅ 抓到；另 warning 67（p95 跳桶） |
| `recurring_override` mute、`z=1000` | ta 那一小時不報 | ✅ |
| `recurring_override` `z=2`（比預設嚴） | 不變嚴 | ✅ 13 → 13 個 |
| `recurring_override` `train: exclude`（週一 17 點） | 17 點退到 hour、18 點不變 | ✅ |
| `aiops-tuning` 關閉（讀不到） | detect 照跑、印提示 | ✅（修正前沒有提示） |
| 壞調整（缺 `end`、正規表示式錯） | 略過那筆、其他照常 | ✅（修正前 `KeyError`／`PatternError` 整個失敗） |

**優先順序（程式現行規則）**：`series.yaml` 序列自己的設定 ＜ `series.yaml` 的 `overrides`（檔案順序）＜ `adjustments.yaml` ＜ `aiops-tuning`（依 `at` 由舊到新）。同一欄位後面蓋前面，不同欄位各自生效；**不看 `match` 寫得多細**，較晚寫入的寬鬆調整會蓋掉較早的精細調整。

**測試設計的修正**（第一輪跑完後改，標準沒有放寬）：過期測試原本用「現在 −1 小時」，但 `--at` 在兩天前，以 `--at` 判斷時還沒過期，改成 `--at` −1 小時；排掉週一、週期不學兩項原本選到 10-12 收集中斷的時段（本來就是 hour），改看非中斷時段；壞調整的 detect 沒先清上一輪的異常，加上清除。

**沒測到的**：14 天後降解析度（5 分鐘）的資料（歷史全用 30 秒）；TSDS（改 standard mode）；trace 每一段的 baseline；`backtest`；借同組 baseline（`groups` 沒定義）；真實流量。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| p95 從直方圖算，值是桶中點，MAD＝0、σ 只剩 5% | 誤報率：p95 anomaly 3.52% | 一週 2,133 個誤報，佔全部的 88% | 門檻選擇：σ 至少半個桶寬，或 p95 的 `min_abs` 改相對值；回測後再定 |
| 錯誤率 `min_events` 20、`min_abs` 5%：一個 5xx 就到門檻 | 誤報率：0.34%，`confirm_points 1` 直接 anomaly | 量少的 API 常被報 | 門檻選擇：至少 N 個 5xx 才判斷，或 `min_events` ≥ 60 |
| `hour` 層把平日、週末混在一起 | 10-12 中斷 6 小時後，週一 11 點掉到一成只有 z −4.0；剛上線 3 天 warning 2 | 一次收集中斷讓該時段兩週漏報 | 演算法設計：hour 層分平日／週末；要改公式，先排評估 |
| 尖峰不對齊整點，168 格切不開 | 08:45 起的尖峰每個平日 15 分鐘 anomaly | 一週 92 個誤報 | 演算法設計：邊界附近也比相鄰時段，或用 `recurring_override` |
| 換版的永久改變要人標 `reset_point` | 沒標時一天 2,718 個 anomaly | 換版當天到兩週後一直報 | [④⑨] CI/CD 自動寫 proposed 的 `reset_point` |
| 一週誤報率是合成資料 | — | 真實分布不同 | 正式環境只寫入不通知跑 2～4 週再量 |

- **決定**：四個程式錯誤修掉（收集中斷時補 0、有 filter 的序列判斷收集中斷、壞調整讓整輪失敗、過期的時間點）；門檻與 168 格的設計問題不改，列在上表。
- **下一輪**：p95、錯誤率的門檻用 `backtest` 比較後再定（[README.md](../README.md) 的「用真實資料校正門檻」）。

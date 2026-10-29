# 排除已知干擾：排除時段、重置點、週期放寬（2026-10）

**狀態**：已完成（已合進 master）；沒排程　　**日期**：2026-10-23　　**相關**：PR #7、[baseline 定義的原則與限制](../../../engine/configs/baseline/reference.md#定義的原則與限制)、[餵給 baseline 之前](../../../engine/configs/baseline/reference.md#餵給-baseline-之前擋了什麼還缺什麼)
**模組**：② 清資料（餵給 ④ 判斷之前）

## Plan：這一次要達到什麼

- **需求**：三種情況讓 baseline 學錯或報錯，統計公式（中位數＋MAD）對它們沒有辦法：

| 情況 | 後果 | 對應的調整 |
|---|---|---|
| 事故、維護、壓測的時段 | 被當成「平常」學進去；長事故拉歪中位數 | `exclude_window` |
| 換版後行為變了（例如每個請求慢 200ms） | 新值佔歷史一半（約 2 週）才跟上，期間一直報 | `reset_point` |
| 月底、固定排程的批次 | 一週 168 格的 baseline 學不到，每個月底都誤報 | `recurring_override` |

- **目的**：不改統計公式，用「調整資料」解：哪些資料不學、從哪裡重新學、哪些時段放寬。
- 調整的寫入點預留給之後的 learning loop（值班回饋、CI/CD 自動寫入）。
- **範圍**：做上表三種調整與指令。不做自動寫入、不做排程。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `engine/detector/adjust.py`（新） | 四種 `kind`（含舊的 `override`）；全部是純函式 | 統計公式不動 |
| 同上 | `train_filter`：訓練前拿掉點 | 排除要在學之前 |
| 同上 | `detect_filter`：偵測後拿掉異常 | 放寬要在判斷之後 |
| 同上 | `in_schedule`：cron＋duration＋每月最後一天 | 週期性的時段 |
| 同上 | 欄位別名 | 同一個服務在各序列欄位名稱不同，寫一次就對得到 |
| 同上 | `status: proposed`、`expires_at` | 留給 learning loop：機器提的建議要人核准才生效 |
| `engine/detector/model.py` | `match_keys` 改用 `adjust.match_keys`（支援別名） | 同上（別名） |
| 同上 | `override_for` 只套 `kind: override` | 新的 `kind` 共用 `aiops-tuning`，不能被當成粒度／z 的調整 |
| `engine/detector/es.py` | `load_baseline` 多讀 `trained_at` | `reset_point` 要判斷 baseline 是不是重置前學的 |
| `engine/detector/__main__.py` | `train`：抓到歷史後、`train()` 之前呼叫 `train_filter` | 排除要在學之前 |
| 同上 | `detect`：打分後呼叫 `detect_filter` | 放寬要在判斷之後 |
| 同上 | `trace-train`／`trace-detect` 同樣處理 | — |
| 同上 | 新增 `adjust` 指令（add／list／approve／remove） | — |
| 同上 | `tune` 寫入補上 `kind: override`；`aiops-tuning` mapping 擴充 | 跟 `tune` 共用一個 index，learning loop 只有一個寫入點 |
| `engine/configs/baseline/adjustments.yaml`（新） | 長期規則的設定檔與說明 | 月底批次這類長期規則進版控；事件類用指令寫 ES |
| `engine/tests/test_adjust.py`（新） | 別名、生效條件、cron、各種 filter 的邊界 | — |
| `engine/configs/baseline/README.md`、`docs/pdca` | 文件 | — |

- 別名涵蓋的欄位名：`service`、`service.name`、`resource.attributes.service.name`、`entry.service`。
- `train_filter` 也套用在粗粒度重查與稀少筆數的卜瓦松。
- 事件類調整：事故、換版。

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 別名寫一次涵蓋各序列的欄位名稱 | 單元測試 | `service=mes-api` 對得到四種欄位名稱 |
| 舊的 `tune` 紀錄（沒有 `kind`）照常生效 | 實測 `tune` 後 `adjust list` | 當成 `override`，照常列出 |
| 週期算得對 | 單元測試，給定時間點看是否在時段內 | 二月最後一天（非閏年）、跨午夜、週一到週五、`*/15` 步進都對 |
| cron 寫錯會擋 | 單元測試 | cron 欄位數錯誤會報錯 |
| 長事故沒排除會把平常學歪 | ES 灌 8 天 log，其中 5 天是 3 倍的長事故 | 平常被學成每 5 分鐘 13.57 筆（正常是 5） |
| 排除後回正常 | 同上，排除那 5 天 | 回到正常 |
| `proposed` 不生效 | 3 天前換版、量變 2 倍；寫 `proposed` 的重置點 | 平常仍是舊版（5.36） |
| 重置點讓 baseline 改學新版 | 同上，再 `approve` | 學成新版（10.00） |
| 週期 `mute` 壓得掉異常 | 最近 40 分鐘 log 爆 10 倍（平常 15、現在 450） | 沒有調整時報；加了符合當下的 `mute` 後不報 |
| 只壓符合的對象與時段 | 同上 | 時段外、別的服務不受影響 |
| 調整只會讓異常變少 | 單元測試：本來不是異常的點套用放寬 | 結果不變 |

**發現並修掉的問題**：

| 問題 | 怎麼處理 |
|---|---|
| 重置點事後補標（`reset_at` 在過去）時，舊 baseline 被當成新的 | 以 `reset_at` 與調整寫入時間兩者較晚的為準 |
| 只靠 `min_points: 50`，重置後 25 分鐘就有 baseline，不可信 | 加 `warmup`，重置後預設 24 小時內不判斷 |
| 「排除了幾個點」偏大（印出 45,988） | 只算有值的點，服務上線前的空列不算 |

- 第 1 列：在 `reset_at` 之後、調整寫入之前 train 的 baseline 仍是舊的；實測中爆量的 `long` 仍被報。
- 第 2 列：30 秒一點；實測重新 train 後立刻 detect，出現 30 筆異常（z=560，baseline level `all`、可信度 low）。
- **沒測到**：`trace-train`／`trace-detect` 套用調整只改了程式，沒用真實 trace baseline 實測。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 重置後 24 小時（`warmup`）完全不判斷 | 實測加暖機後，爆量也不報 | 換版當天出事抓不到 | 24 小時是估的，用真實換版資料校；可考慮暖機期間只靠規則（重啟、OOM） |
| `recurring_override` 只能放寬，不能變嚴 | `detect_filter` 是打完分後拿掉異常，不改 `score()` | 要調嚴只能用 `tune` | 文件註明 |
| [②] 資料品質檢查（`quality`）不套用這些調整 | 設計：它要看全部資料才抓得到壞資料 | 維護期間的資料停了仍會報 | 評估要不要讓維護時段靜音 |
| [⑧] `proposed` 沒人核准就永遠不生效，也沒有提醒 | 推論（`approve` 才改 status，沒有通知） | 建議可能被忘記 | 告警出口做好後，列出待核准的建議 |
| [④⑨] 沒有排程；CI/CD 換版時不會自動寫 `reset_point` | — | 換版要人記得標 | 串 CI/CD、值班回饋 |
| 別名表要隨新序列維護 | 實作時：序列各用不同欄位名稱 | 新序列用新欄位名稱時 `match` 對不到 | 加序列時檢查 `ALIASES` |
| [④] 限制 2（指標不夠）、4（`groups` 沒定義）沒處理 | 見 [baseline 定義的原則與限制](../../../engine/configs/baseline/reference.md#定義的原則與限制) | — | 待排 |

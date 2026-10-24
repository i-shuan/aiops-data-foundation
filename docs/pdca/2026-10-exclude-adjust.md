# 排除已知干擾：排除時段、重置點、週期放寬（2026-10）

**狀態**：已完成（已合進 master）；沒排程　　**日期**：2026-10-23　　**相關**：PR #7、[baseline 正式定義](2026-10-baseline-definition.md)、[清資料盤點](2026-10-data-cleaning.md)

## Plan：這一次要達到什麼

- **需求**：三種情況讓 baseline 學錯或報錯，而統計公式（中位數＋MAD）對它們沒有辦法：
  1. 事故、維護、壓測的時段被當成「平常」學進去（長事故拉歪中位數）。
  2. 換版後行為變了（例如每個請求慢 200ms），要等新值佔歷史一半（約 2 週）baseline 才跟上，期間一直報。
  3. 月底、固定排程的批次，一週 168 格的 baseline 學不到，每個月底都誤報。
- **目的**：不改統計公式，用「調整資料」解這三件事：哪些資料不學、從哪裡重新學、哪些時段放寬。而且調整的寫入點要預留給之後的 learning loop（值班回饋、CI/CD 自動寫入）。
- **範圍**：做 `exclude_window`、`reset_point`、`recurring_override` 三種調整與指令。不做自動寫入、不做排程。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `aiops/aiops_baseline/adjust.py`（新） | 四種 `kind`（含舊的 `override`）；`train_filter`（訓練前拿掉點）、`detect_filter`（偵測後拿掉異常）、`in_schedule`（cron＋duration＋每月最後一天）、欄位別名、`status: proposed`／`expires_at` | 全部做成純函式，統計公式不動；`proposed` 是留給 learning loop 的接口：機器提的建議要人核准才生效 |
| `aiops/aiops_baseline/model.py` | `match_keys` 改用 `adjust.match_keys`（支援別名）；`override_for` 只套 `kind: override` | 新的 `kind` 共用 `aiops-tuning`，不能被當成粒度／z 的調整；同一個服務在各序列欄位名稱不同（`service`、`service.name`、`resource.attributes.service.name`、`entry.service`），要寫一次就對得到 |
| `aiops/aiops_baseline/es.py` | `load_baseline` 多讀 `trained_at` | `reset_point` 要判斷某個 baseline 是不是重置之前學的 |
| `aiops/aiops_baseline/__main__.py` | `train`：抓到歷史後、`train()` 之前呼叫 `train_filter`（粗粒度重查與稀少筆數的卜瓦松也要）；`detect`：打分後呼叫 `detect_filter`；`trace-train`／`trace-detect` 同樣處理；新增 `adjust` 指令（add／list／approve／remove）；`tune` 寫入補上 `kind: override`；`aiops-tuning` mapping 擴充 | 排除要在學之前；放寬要在判斷之後；跟 `tune` 共用同一個 index，learning loop 只有一個寫入點 |
| `aiops/baseline/adjustments.yaml`（新） | 長期規則的設定檔與說明 | 月底批次這類長期規則進版控；事件類（事故、換版）用指令寫 ES |
| `aiops/tests/test_adjust.py`（新） | 別名、生效條件、cron、各種 filter 的邊界 | — |
| `aiops/baseline/README.md`、`docs/pdca` | 文件 | — |

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| 別名寫一次是否涵蓋各序列的欄位名稱；舊的 `tune` 紀錄（沒有 `kind`）是否照常生效 | 單元測試；實測 `tune` 後 `adjust list` | `service=mes-api` 對得到四種欄位名稱；舊紀錄當成 `override`，照常列出 |
| 週期算得對不對：每月最後一天、跨午夜、星期幾、`*/15` | 單元測試，給定時間點看是否在時段內 | 二月最後一天（非閏年）、跨午夜、週一到週五、步進都對；cron 欄位數錯誤會報錯 |
| 長事故沒排除會不會把平常學歪，排除後會不會回正常 | ES 灌 8 天 log，其中 5 天是 3 倍的長事故；比較排除前後學到的平常 | 沒排除：平常被學成每 5 分鐘 13.57 筆（正常是 5）；排除那 5 天後回到正常 |
| 換版後，重置點能不能讓 baseline 改學新版；`proposed` 是否真的不生效 | 3 天前換版、量變 2 倍；先寫 `proposed` 的重置點，再 `approve` | `proposed` 時平常仍是舊版（5.36）；`approve` 後學成新版（10.00） |
| 週期放寬／靜音是否真的壓掉異常，且只壓符合的對象與時段 | 最近 40 分鐘 log 爆 10 倍（平常 15、現在 450）；加上符合當下的週期 `mute` | 沒有調整時報；加了 `mute` 後不報；時段外、別的服務不受影響 |
| 調整只會讓異常變少，不會憑空多出來 | 單元測試：本來不是異常的點套用放寬 | 結果不變 |

測的過程中**發現並修掉的問題**：

| 問題 | 怎麼發現的 | 怎麼處理 |
|---|---|---|
| 重置點事後補標（`reset_at` 在過去）時，在 `reset_at` 之後、但在這筆調整寫進來之前 train 的 baseline 仍是舊的，卻被當成新的 | 實測加了過去時間的重置點後 detect，爆量的 `long` 仍被報 | 以 `reset_at` 與調整寫入時間兩者較晚的為準 |
| 只靠 `min_points: 50`，重置後 30 秒一點只要 25 分鐘就有 baseline，太短、不可信 | 實測重新 train 後立刻 detect，出現 30 筆異常（z=560，baseline level 是 `all`、可信度 low） | 加 `warmup` 欄位，重置後預設 24 小時內不判斷 |
| 「排除了幾個點」的數字偏大 | 兩筆調整都生效時印出「排除 45,988 個點」，遠大於實際有值的點 | 排除的點數只算有值的，服務上線前的空列不算 |

沒測到：`trace-train`／`trace-detect` 套用調整只做了程式修改，沒有用真實 trace baseline 實測。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| 重置後 24 小時內（`warmup`）完全不判斷，這段時間真的壞了也不報 | 實測加暖機後，爆量也不報 | 換版當天出事抓不到 | 24 小時是估的，要用真實的換版資料校；之後可考慮暖機期間只靠規則（重啟、OOM）把關 |
| `recurring_override` 只能放寬，不能變嚴 | 寫 `detect_filter` 時：它是在打完分後拿掉異常，不改 `score()` | 要調嚴只能用 `tune` | 文件註明 |
| 資料品質檢查（`quality`）不套用這些調整 | 設計：它要看全部資料才抓得到壞資料 | 維護期間的資料停了仍會報 | 評估要不要讓維護時段靜音 |
| `proposed` 沒人核准就永遠不生效，而且沒有提醒 | 推論（`approve` 才改 status，沒有任何通知） | 建議可能被忘記 | 告警出口做好後，列出待核准的建議 |
| 沒有排程；CI/CD 換版時不會自動寫 `reset_point` | — | 換版還是要人記得標 | 串 CI/CD、值班回饋 |
| 別名表要隨新序列維護 | 實作時發現序列各用不同欄位名稱 | 新序列若用新的欄位名稱，`match` 對不到 | 加序列時檢查 `ALIASES` |
| 限制 2（指標不夠）、4（`groups` 沒定義）沒處理 | 見 [baseline 正式定義](2026-10-baseline-definition.md) | — | 待排 |

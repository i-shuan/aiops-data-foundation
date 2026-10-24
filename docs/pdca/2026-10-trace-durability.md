# trace 收值線要不要 message queue（2026-10）

**狀態**：進行中（Plan）　　**日期**：2026-10-24　　**相關**：分支 `claude/collect-time-mq`
**模組**：① 收值（trace 這條線）

## Plan：這一次要達到什麼

- **需求**：log、metrics 送 ES 時，佇列寫在節點磁碟上（`file_storage`），Collector 重啟、ES 停機都不會掉。trace 這條線不一樣，讀設定看到三個可能掉資料的地方（**都還沒實測**）：
  1. node Collector → trace-sampler（`loadbalancing/traces`）：佇列在**記憶體**；重試 `max_elapsed_time: 120s`，trace-sampler 掛超過 2 分鐘就丟；node Collector 重啟，佇列裡的直接沒了。
  2. trace-sampler 本身：等 30 秒（`DECISION_WAIT`）收齊整個 trace 才決定，這段在**記憶體**；trace-sampler 重啟就沒了。
  3. trace-sampler → ES：佇列在**記憶體**（沒有 `storage`）；ES 停機久了佇列滿了丟，trace-sampler 重啟也沒了。
  有問題的 trace（錯誤、慢）全留，是 ⑦ 根因分析最需要的；剛好出事時（重啟、擴縮、ES 慢）最容易掉。
- **目的**：用實測量出每一種情況會掉多少，再比較兩種做法，決定要不要加 message queue：
  - **A. 磁碟佇列**：node 端與 trace-sampler 的佇列加 `file_storage`（trace-sampler 是 StatefulSet，可以掛 PVC）。
  - **B. Kafka**：node Collector 寫 Kafka（依 trace_id 分 partition），trace-sampler 從 Kafka 讀。
- **範圍**：用 docker 模擬兩層 Collector（版本跟正式一樣 0.162.0）＋ ES 9.5.5，量掉多少；A 實作並驗證；B 先評估，量得到的就實測。不改正式環境。

## Do：改動哪些 code、改了什麼、為何要改

（進行中）

## Check：測試的目的與結果

（進行中）

## Act：Check 後沒改善的、還沒解決的

（進行中）

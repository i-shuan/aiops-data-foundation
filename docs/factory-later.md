# 工廠部分（之後再做）

**目前的範圍只有一般服務**（Web API、worker、Kafka、DB）：協定標準、呼叫鏈單純，先把整套流程跑通。等這套流程成熟，再接工廠。底層（ES、OpenTelemetry Collector、baseline、根因分析）共用，到時候主要是加解析規則、換欄位、加序列，不用重寫。

這份把散在各文件裡跟工廠有關的內容集中在一起，主線文件只留一行連結過來。

## 已經做好、之後可以直接用的

| 項目 | 狀態 | 在哪裡 |
|---|---|---|
| 業務欄位名稱：`lot.id`（批號）、`equipment.id`（機台）、`work_order.id`（工單）、`recipe.id`（配方） | ✅ 定案 | [sdk/standards/tracing.md](../sdk/standards/tracing.md) |
| 機台指標的命名提案，加上 SECS 斷線、PLC 掃描週期超規、重傳三條規則 | ✅ 用模擬 exporter 在 k3s 驗過 | [metrics/cases.md 案例 4](metrics/cases.md)、`engine/configs/baseline/rules.yaml`（`eap.*`） |
| 很稀少的筆數（設備告警一小時 0～2 次）改用卜瓦松 | ✅ 模擬資料驗過（一小時多 8 次抓到） | [baseline README「自動調整」(2)](../engine/configs/baseline/README.md#自動調整粒度低頻事件借用-baseline) |
| 每條序列依事件數自己選粒度（低頻的 SECS 事件自動往粗調） | ✅ | 同上 (1) |

## 還沒做的

### 1. log 解析

- **SECS 的 log 解析規則**：需要真實 log 樣本，包括一般事件、告警、逾時、多行 body。SECS 一則跨多行，Collector 收的時候要先合併。
- 解析規則寫在中央 pipeline 的 `formats/`，做法跟其他格式一樣（[log/README.md「pipeline」](log/README.md#pipeline)）。

### 2. 業務 ID 與跨系統串接

**欄位**：

| 欄位 | 狀態 |
|---|---|
| `lot.id`、`equipment.id`、`work_order.id`、`recipe.id` | ✅ 定案 |
| 載具（FOUP）、腔體、交易 ID（SECS SystemBytes）、搬送指令 ID、EAP 實例 | ❌ 還沒定，要先跟平台、MES、EAP 團隊確認 |

**MES → MCS → AMHS 斷鏈**：這些系統之間常常不是 HTTP，而是用資料庫當佇列、交換檔案、或用廠商協定，`traceparent` 傳不過去。務實的做法：

1. **盤點交換點**：每個交換點標成「有 trace_id」「有業務 ID」或「兩者都沒有」。
2. **用業務 ID 事後關聯**：各系統的 log 都記工單號、載具 ID、搬送指令 ID，用這些 ID 接起來。
3. **能改的交換表加 `trace_id` 欄位**。
4. **時間線工具**：輸入一個批號，列出它經過 MES → MCS → EAP 的所有 log 和 trace，依時間排序。

**不改程式的路**：中央 pipeline 依各系統的 log 格式，把 `LOT=ABC123` 這類字串抽成標準欄位。舊系統不用改，只要 log 裡有寫。

**一定要注意**：業務 ID 只放 log、trace，**不放 metrics**（每個值一條序列，每天幾萬個批號會讓序列數爆炸）。

### 3. EAP 的 trace

- EAP 不是 HTTP，自動埋點抓不到。EAP 程式要在送出、收到每一則 SECS 訊息時手動開 span，帶上機台、指令（S6F11…）、交易 ID。
- **trace 只到 EAP 為止**：機台不會把 `trace_id` 傳回來。EAP 的 log 一定要寫 `trace_id`，才能從機台事件對回原本的請求。

### 4. 機台的 baseline 與規則

| | 一般服務 | EAP／機台 |
|---|---|---|
| 串起整件事的 ID | `trace.id`、`work_order.id` | `lot.id`、載具 ID、交易 ID |
| 對象 | `service.name`、Pod | `equipment.id`、腔體、EAP 實例 |
| 請求與回應 | HTTP 請求 → 200／500 | SECS 主訊息（例如 S2F41）→ 回覆（S2F42）與 ACK |
| 延遲 | API 回應時間 p95、p99 | 機台交握耗時、T3 逾時 |
| 異常 | 5xx 突增、慢查詢 | 機台告警（S5F1）、命令被拒（HCACK ≠ 0） |

- 在 `series.yaml` 加機台交握耗時、逾時次數、告警次數，依 `equipment.id` 切。程式不用改。
- 要 EAP 團隊照命名提案開出機台指標（exporter）。
- OEE 由 MES、EAP 算好吐出來，不在這裡算。

### 5. 時間對齊

- EAP 主機（多半是 Windows）、MES DB 主機也要監控時鐘偏移（windows_exporter 的時間偏移），規則跟 K8s 節點一樣：偏移 > 100ms 報 warning、> 1 秒報 critical。
- 機台本身的時間，依 EAP 收到訊息的時間為準。

### 6. 行事曆

- 歲修、盤點、換線、停機保養的時段，不判斷，也不學進 baseline。
- 一般服務也需要行事曆（國定假日），做法共用，見 [rca/README.md「髒資料排除」](rca/README.md#髒資料排除)。

### 7. 機台、製程的模型（MLOps）

- 一般服務這層定案不自己訓練模型（[feedback/llm-mlops.md](feedback/llm-mlops.md)）。
- 機台、製程的多變量資料（預測保養、FDC、虛擬量測、良率）才可能需要。先問設備、製程部門有沒有現成的，這個平台提供 IT 層的資料（EAP 通訊、交握耗時、主機狀態）給他們用。

## 什麼時候開始

一般服務這邊跑穩之後再開始，也就是做到下面這三點：

1. 正式排程上線。
2. 收斂與告警出口完成。
3. 用 2～4 週真實資料校正過門檻。

開始時的第一步：

- 拿到 SECS 的真實 log 樣本。
- 跟 EAP、MES 團隊定好剩下的業務 ID 欄位。

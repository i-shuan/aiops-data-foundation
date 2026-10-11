# trace 能做什麼：六個場景對照現況

一句話：**六個場景裡，慢在哪、程式壞味道、錯誤源頭這三個已經做了而且實測過（含不用學的 N+1、吞掉例外規則）；取樣改成尾端取樣，出事的請求全部留下。非同步（Kafka）不改程式做到一半（排隊時間、消費者變慢出錯抓得到，但串不回是哪一筆訊息）；架構治理、工廠（SECS、MES→MCS→AMHS）還沒做。**

trace 怎麼收、怎麼分部門見 [traces.md](traces.md)；baseline、根因分析的實作見 [aiops/baseline](../aiops/baseline/README.md#trace-每一段的-baseline)、[aiops/rca](../aiops/rca/README.md)。

## 對照表

| 場景 | 回答的問題 | 現況 | 缺的 |
|---|---|---|---|
| **一、慢在哪** | 慢在程式本身、下游還是 DB？ | ✅ 根因分析：每一段只算自己的時間（重疊的子呼叫只算一次），出事時跟平常比，多最多的排第一；trace baseline 每一段的 `time_ms` | 循序 vs 並行、建連線耗時（見下） |
| **二、程式壞味道** | N+1、重試太多、快取穿透？ | ✅ trace baseline 每一段的 `calls`（每個請求呼叫幾次），k3s 實測 N+1、重試風暴、快取穿透都抓到；✅ 不用學的規則 `repeated`（同一個呼叫 ≥ 20 次），一開始就有的 N+1 也抓得到 | — |
| **三、錯誤源頭** | 鏈路裡哪一台先爆？ | ✅ 根因分析：標了錯誤、底下子呼叫都沒錯的那段才是源頭；trace baseline 每一段的 `error_rate`；✅ 吞掉的例外：規則 `swallowed` ＋ baseline `swallowed_rate` | — |
| **四、非同步、訊息佇列** | 訊息在 Kafka 排了多久？ | ⚠️ 真的 Kafka＋.NET 實測（不改程式）：排隊時間、消費者寫 DB 的變慢／出錯、lag 都抓得到 | 不改程式時消費端串不回訊息；正式環境的排隊時間大多算不出來（見下） |
| **五、架構治理** | 實際的相依長怎樣？有沒有循環呼叫？ | ⚠️ 根因分析會畫呼叫關係圖；`trace.segment.new` 報「這支 API 出現沒看過的呼叫」 | 循環呼叫、越權直連 DB、舊 API 還有誰在叫 |
| **六、工廠** | 機台交握卡在哪一道指令？工單卡在 MES、MCS、AMHS 哪一段？ | ❌ | EAP 要手動埋點；跨異質系統怎麼串（見下） |

## 各場景還缺什麼

### 一、慢在哪

- **循序 vs 並行**：三個外部呼叫本來可以同時發，被寫成一個接一個。trace 看得出來：子呼叫彼此不重疊、一個結束下一個才開始，而且彼此沒有相依（不同的下游）。可以做成規則：「這支 API 每個請求有 N 段互不重疊的外部呼叫，加起來佔總時間 80% 以上」。**要先確認誤報**：有些本來就必須照順序（先查訂單才能查明細）。
- **建連線、冷啟動**：自動埋點通常**沒有** TCP／TLS 建連線的 span（Java agent 沒有；.NET 9 的 HttpClient 有實驗性的 `http.connection_setup`）。看得到的是「第一個請求特別慢」。要做的話先確認公司的版本有沒有這種 span。

### 二、程式壞味道

- ✅ **一開始就有的壞味道**：baseline 是跟「平常」比，N+1 如果從上線第一天就在會被學成正常。所以加了不用學的規則 `trace.segment.repeated`：同一個請求裡，同一個往外的呼叫（同一句 SQL、同一個下游）平均 ≥ 20 次就報（[trace.yaml](../aiops/baseline/trace.yaml) 的 `rules.repeated_calls`）。k3s 實測：「同一個呼叫太多次（可能是 N+1）：rca-wip-worker GET /wip → … SELECT LOT_HISTORY → oracle（每個請求 100 次，門檻 20）」。這種規則可以在測試環境就跑，上線前抓出來。
- 快取穿透目前看「查 DB 的次數變多」；平常就穿透的也一樣學不出來，同上。

### 三、錯誤源頭

- ✅ **吞掉的例外（假性健康）**：程式 catch 掉例外、回傳空值和 200，span 帶例外事件但 status 不是 error。實測發現 **ES 的 OTLP 入口不存 span 的事件**，所以 node Collector 先把例外的種類、訊息抄到 span 屬性（`exception.type`、`exception.message`）。然後每一段算「丟了例外但沒標成錯誤」的請求比例：學 baseline（`swallowed_rate`，平常就有一點的看變多沒有），另外不用學的規則 `swallowed` ≥ 5% 就報。k3s 實測：「吞掉的例外：rca-wip-worker GET /wip → … SELECT LOT_HISTORY → oracle（100% 的請求丟了 System.TimeoutException，但沒標成錯誤）」。尾端取樣也會把這種 trace 全部留下（規則 `exception`）。

### 四、非同步、訊息佇列

用真的 Kafka（單台，KRaft）＋ 兩個 .NET 8 服務實測，**只掛自動埋點、不改程式**（[test/local-k3s/kafka](../test/local-k3s/kafka)）：order-producer 收到 HTTP 請求就把訂單送進 Kafka，order-consumer 讀出來寫進 PostgreSQL。

**.NET 自動埋點（不改程式）串得起來的程度：**

| | 結果 |
|---|---|
| 生產端：HTTP 請求 → `orders publish` | ✅ 同一個 trace |
| 消費端拿到訊息（`orders receive`） | ⚠️ **另起一個新的 trace**，只用 **link** 指回生產端的 publish |
| 消費端處理時寫 DB | ❌ **每一筆都自成一個 trace**（實測 944／944 筆沒有上一層），對不回是哪一筆訊息 |
| `receive` 的時間 | 是「`Consume()` 等下一筆訊息等了多久」，不是處理時間 |
| kafka_exporter 的名稱 | ✅ `kafka_consumergroup_lag_sum{consumergroup, topic}`，跟 [rules.yaml](../aiops/baseline/rules.yaml) 的 Kafka 規則一樣 |

**不改程式時平台怎麼補：**（[aiops/baseline](../aiops/baseline/README.md#trace-每一段的-baseline)）

- **排隊時間**：消費端拿到訊息的時間（receive 結束）− 生產端送出的時間（publish 結束），生產端用 link 找回來。trace baseline 多一段「`order-consumer › 排隊（orders）← order-producer`」，平常 0.2ms。
- **處理時的 DB**：沒有上一層的 span 也當成入口，所以「`order-consumer › postgres → postgresql`」自己有 baseline（平常 1.4ms）、錯誤率。看得出「這個消費者寫 DB 變慢、出錯」，只是對不到是哪一筆訊息。
- **積壓**：kafka_exporter 的消費落後（lag）＋ Kafka 規則。

**實測（`run-kafka-e2e.sh`）：**

| 案例 | 抓到的 |
|---|---|
| 正常 3 分鐘 | 沒有誤報 ✅ |
| 消費者變慢（每筆多 150ms，跟不上每秒 10 筆） | 「排隊」那段時間 ✅；lag 10 → 399 筆 ✅ |
| 寫 DB 失敗（程式 catch 掉、記 error log） | 消費者的 `postgres` 那段錯誤率 ✅ |

**限制：**

1. **正式環境的排隊時間大多算不出來。** 不改程式時生產端、消費端是兩個 trace，尾端取樣各自決定留不留；正常的只留 5%，兩邊剛好都留下的很少，找不到生產端就算不出來（測試環境全部留，所以測得到）。積壓要靠 lag。
2. **消費端出問題時對不到是哪一筆訊息。** 要查哪一筆，用 log（消費者的 log 有訂單編號、時間）。
3. **讓整條串起來要改程式**：消費端在「拿到訊息之後」用 header 裡的 `traceparent` 開一個處理的 span（約 5 行，.NET 內建的 `System.Diagnostics`，不用加套件），處理時的 DB、呼叫就會接在生產端的 trace 底下，排隊時間也一定算得出來。實測過可以，但**目前決定先不改程式**。
4. 沒有測：Java agent（它的消費端行為不同）、Java ↔ .NET 互傳、RabbitMQ 等其他佇列、批次工作（排程）。

### 五、架構治理

都從 trace 統計就算得出來；`aiops-trace-segments` 已經是每支 API 往外呼叫誰的統計，可以留很久（trace 原文只留 7 天）。

- **循環呼叫**：從呼叫關係找環（A → B → C → A）。
- **越權直連 DB**：先定義規則（例如「只有 wip-worker 能連 MES 的 Oracle」），呼叫關係裡違反的就報。規則要由架構負責人定。
- **舊 API 還有誰在叫**：某支 API 標成「要下架」，看最近 30 天還有哪些服務在呼叫。

### 六、工廠

- **SECS／GEM 交握**：EAP 不是 HTTP，自動埋點不會有。要 EAP 程式在送出、收到每一則 SECS 訊息時手動開 span，帶上機台、指令（S6F11…）、交易 ID（SystemBytes）。這是 [README 待辦第 3 點](../README.md#待辦)的階段 2。
- **MES → MCS → AMHS**：這些系統之間常常不是 HTTP（資料庫當佇列、檔案、廠商協定），`traceparent` 傳不過去。務實的做法是**用業務 ID 串**（工單號、載具 ID、搬送指令 ID）：每個系統的 span 或 log 都帶上同一個 ID，查的時候用 ID 把各段接起來，不一定要是同一個 trace。

## 橫跨所有場景的前提：取樣

現在在應用裡、請求一開始就決定要不要記（例如記一成），那時還不知道這個請求會不會慢、會不會錯。調到一成時，**出事的請求也只剩一成**，場景一、三最需要的剛好是那些。

| | 現在：一開始就決定 | 另一種：結束後才決定 |
|---|---|---|
| 怎麼做 | 應用依比例決定 | 先全部送到 Collector，整個 trace 到齊後決定：錯的、慢的全留，正常的留一成 |
| 出事的請求 | 只留一成 | **全留** |
| 架構 | 現在這樣 | 同一個 trace 的所有段要到同一台 Collector：多一層「依 trace_id 分配」的 Collector，要等 trace 到齊（例如 10 秒）才決定，Collector 要更多記憶體 |
| 應用端負擔 | 低（沒記的不送） | 每個請求都要送到 Collector |

要先決定：高流量服務打算記幾成？全部記的話不用改；要調低的話，建議評估「結束後才決定」。

**k3s 實測（結束後才決定）**：兩層 Collector（node → 依 trace_id 分配 → trace-sampler × 2），壓測每秒 500 個請求、11% 有問題（[test/local-k3s/tail-sampling](../test/local-k3s/tail-sampling/README.md)）：

- 錯誤、慢、吞掉例外（回 200 但有例外事件）**100% 留下、每個都完整**；正常的留 5%，總量剩約 15%。
- 隨機那 5% 錯的慢的也一樣抽，**trace baseline 只用這份就不會偏**（每一段記著是哪條規則留的）。
- **decision_wait 要比最長的請求長**：等 10 秒時，「下游很快、自己之後又做了 12 秒」的請求只留下 5%（決定時還看不到最上面那段）；等 30 秒就全部留下。
- 資源：每秒 2,000 段，trace-sampler 每台約 150MB、0.02 核。

**已經放進正式設定**（[platform/cluster-addons/otel-collector](../platform/cluster-addons/otel-collector)，流程見 [traces.md](traces.md)）：等 30 秒、隨機 5%、慢的門檻 1 秒、trace-sampler 2 台；應用保持全部送（`otel.traces.sampleRatio: 1.0`，預設）。trace baseline 只抽隨機那份（`sample_policy: random`）。測試環境預設全部留（random 100%），其他 e2e 的 trace 數量不變；尾端取樣的實測自己把它調回 5%。

## 建議順序

1. ✅ **取樣**：尾端取樣已放進正式設定。
2. ✅ **場景二、三的補強**：N+1、吞掉的例外的規則。
3. ⚠️ **場景四**：不改程式的版本做完（見上）；要整條串起來得改消費端程式，先不做。
4. **場景五**：循環呼叫、越權直連 DB（規則要人定）。
5. **場景六**：跟 EAP 團隊談手動埋點、跨系統用哪個業務 ID 串。

# 指標的四個領域：典型案例與實測

一句話：**四個領域（基礎架構、應用、DB 與佇列、機台）各拿一個典型故障，在 k3s 上真的做出來，確認現在的資料與規則抓得到；做不到的列在最後。**

腳本：[test/local-k3s/run-metrics-cases-e2e.sh](../test/local-k3s/run-metrics-cases-e2e.sh)。資料都走正式的路線（應用 → OTel Collector → ES），判斷用 [aiops/baseline/rules.yaml](../aiops/baseline/rules.yaml)。

## 四個領域看什麼

| 領域 | 方法 | 看什麼 | 這個 repo 怎麼收 |
|---|---|---|---|
| **基礎架構、K8s** | USE：使用率、飽和度（排隊）、錯誤 | CPU、記憶體、磁碟、網路；**CPU 被限流**；OOM、重啟 | kubelet（kubeletstats）、K8s 物件狀態（k8s_cluster）、**kubelet 的 cAdvisor（只收限流）** |
| **應用** | RED：請求數、錯誤、延遲；再加 JVM | 每個 API 的請求數、5xx 比例、p95／p99；heap、GC | OpenTelemetry Java agent（app-common chart 自動掛），Prometheus 格式 |
| **DB 與佇列** | 飽和度 | 連線池使用中／上限、排隊；Kafka 消費落後 | 連線池：Java agent；Kafka：kafka_exporter（要另外部署） |
| **機台、產線** | 狀態、規格上下限 | SECS 通訊、PLC 掃描週期、重傳、製程參數 | EAP 的 exporter（**要 EAP 團隊開**，名稱見下面的提案） |

## 案例與實測結果

符號：✅ 實測抓到　🔶 用模擬的 exporter（測試環境沒有 Kafka、機台）

### 1. 基礎架構：CPU 使用率不高，延遲卻變長（CPU 被限流）

| | |
|---|---|
| 現象 | 服務 CPU 只有四成，回應時間卻變長 |
| 原因 | 設了 CPU limit。瞬間突波碰到「每 100ms 只能用 N ms」的配額，被凍結到下一個週期。平均看不出來 |
| 看的指標 | `container_cpu_cfs_throttled_periods_total` ÷ `container_cpu_cfs_periods_total`（被限流的週期比例） |
| 原本缺 | kubelet 的 stats 沒有這兩個數字。**這次在 Collector 加了抓 kubelet 內建的 cAdvisor**，只留這兩個指標（cAdvisor 一個節點有上千種，其他不要） |
| 規則 | `container.cpu_throttled`：5 分鐘內被限流的週期 ≥ 25% |
| 處置 | 調高 CPU limit，或拿掉 limit 只留 request |

**實測 ✅**：`cpu-burst`（limit 200m，每 0.5 秒 4 個程序同時跑一下）。平均 CPU **0.071 核，只有 limit 的 35%**，**50% 的週期被限流** → 規則觸發。

### 2. 應用：記憶體洩漏，延遲一路變長

| | |
|---|---|
| 現象 | API 延遲逐小時變長，流量沒變 |
| 原因 | 全域快取一直放、從來不清。老年代越來越滿，Full GC 越來越頻繁、越來越久，GC 時整個程式停住 |
| 看的指標 | **老年代「GC 之後」用量 ÷ 上限**（`jvm_memory_used_after_last_gc_bytes` ÷ `jvm_memory_limit_bytes`，只看 Old／Tenured）；**GC 佔掉的時間**（`jvm_gc_duration_seconds` 的 sum ÷ 經過的秒數） |
| 為什麼看「GC 之後」 | 只看 used 會被還沒回收的垃圾騙（GC 前本來就高）；GC 之後還很滿，才是真的清不掉 |
| 規則 | `jvm.old_gen_after_gc_high`：≥ 80%，連續 2 分鐘；`jvm.major_gc_frequent`：5 分鐘內 major GC ≥ 5 次；`jvm.gc_overhead`：3 分鐘內 ≥ 10% 的時間在 GC（heap 大的服務才有用） |
| 處置 | 先重開止血；之後抓 heap dump 給開發找誰一直放東西。**JVM 要加 `-XX:+ExitOnOutOfMemoryError`**（見下面「實測時發現的事」） |

**實測 ✅**（第二次）：見下方「實測紀錄」。用**真的 Java 程式**（[java-app/OrderApp.java](../test/local-k3s/java-app/OrderApp.java)）掛**真的 OpenTelemetry Java agent 2.20.0**，每個請求往不會清的 list 放 64KB。

### 3. DB 與佇列：訂單建好了，推播 10 分鐘後才到

| | |
|---|---|
| 現象 | 前端訂單已建立，使用者很久之後才收到通知 |
| 原因 | 消費訊息的服務在等 DB 連線（連線池滿了），處理變慢，Kafka 裡的訊息越積越多 |
| 看的指標 | 連線池：使用中 ÷ 上限（`db_client_connections_usage{state=used}` ÷ `db_client_connections_max`）、排隊中的請求（`db_client_connections_pending_requests`）；Kafka：`kafka_consumergroup_lag_sum` |
| 規則 | `db.pool_exhausted`：使用中 ≥ 90% 上限，1 分鐘；`db.pool_waiting`：有人排隊，1 分鐘；`kafka.consumer_lag`：落後 ≥ 10,000 筆，3 分鐘；`kafka.consumer_lag_growing`：5 分鐘內多了 5,000 筆以上 |
| 處置 | 先找出連線拿了不還的地方（trace 看哪個請求拿著連線很久）；暫時擴大連線池、增加消費者 |

**實測**：連線池 ✅ 用**真的 HikariCP**（同一個 Java 程式，打開開關後拿了連線不還）；Kafka 🔶 用模擬的 exporter（照 kafka_exporter 的格式，每 5 秒多 1,500 筆落後）。結果見下方。

**注意**：連線池的「使用中」和「上限」存在 ES 的不同文件（維度不一樣：使用中多了 `state`）。規則引擎這次加了 `join`：同一個 Pod、同一個連線池、同一個時間點的上限補進來再相除。

### 4. 機台：搬送系統取放晶圓時一直 Handshake Timeout

| | |
|---|---|
| 現象 | 搬送系統（AMHS／OHT）在特定機台取放時頻繁逾時 |
| 原因 | PLC 掃描週期從 8ms 抖到 120ms（韌體定時器衝突），通訊跟著重傳 |
| 看的指標 | SECS 通訊狀態、PLC 掃描週期、重傳次數，**依機台（`equipment_id`）分開** |
| 規則 | `eap.secs_disconnected`：斷線 1 分鐘；`eap.plc_scan_slow`：掃描週期 ≥ 規格上限（50ms），2 分鐘；`eap.secs_retransmits`：5 分鐘內重傳 ≥ 10 次 |
| 處置 | 通知設備工程師更新 PLC 邏輯、重置通訊埠 |

**實測 🔶**：模擬 8 台機台，EQP-07 斷線、EQP-03 掃描 120ms 並且重傳。結果見下方。

**機台的指標名稱是平台提案**，EAP 的 exporter 要照這個開（標準沒有規定，先統一才能共用規則）：

| 指標 | 型別 | 維度 | 意思 |
|---|---|---|---|
| `eap_secs_communicating` | gauge | `equipment_id` | 1 通訊中、0 斷線 |
| `plc_scan_cycle_time_ms` | gauge | `equipment_id` | PLC 掃描週期（ms） |
| `eap_secs_retransmits_total` | counter | `equipment_id` | 累計重傳次數 |

機台只有幾百台，`equipment_id` 當維度沒問題（批號 `lot.id` 不行：值太多，放 trace）。

## 實測紀錄

`run-metrics-cases-e2e.sh`，2026-10-10，k3s 單節點：

| 案例 | 情境 | 觸發的規則與數字 | 結果 |
|---|---|---|---|
| 正常時 | 4 個應用都正常（cpu-burst 除外，它從一開始就是壞的） | 沒有 JVM、連線池、Kafka、機台告警 | ✅ 0 誤報 |
| 1 CPU 限流 | cpu-burst：limit 200m、突波 | `container.cpu_throttled`：**50%** 的週期被限流（平均 CPU 0.071 核 = limit 的 **35%**） | ✅ |
| 3 連線池 | order-app 拿了連線不還（真的 HikariCP，上限 5） | `db.pool_exhausted`：使用中 **100%**；`db.pool_waiting`：排隊 1 個 | ✅ |
| 3 Kafka | order-push 消費者卡住（模擬） | `kafka.consumer_lag`：落後 **90,010** 筆；`kafka.consumer_lag_growing`：5 分鐘多 **81,000** 筆；正常的 report 不報 | ✅ 🔶 |
| 4 機台 | EQP-07 斷線、EQP-03 掃描 120ms＋重傳（模擬） | `eap.secs_disconnected`：EQP-07；`eap.plc_scan_slow`：EQP-03 **120ms**（規格 50）；`eap.secs_retransmits`：EQP-03 **270 次**；其他 6 台不報 | ✅ 🔶 |
| 2 記憶體洩漏 | order-app 每個請求漏 12KB（真的 Java＋agent，heap 128MB） | `jvm.old_gen_after_gc_high` 第 13 分鐘、`jvm.major_gc_frequent` 第 14 分鐘、OOM 後 `container.restarted` 第 17 分鐘（過程見下面） | ✅（第二次） |

### 案例 2 第一次沒抓完整：洩漏太快，JVM 變成「活著但什麼都做不了」

第一次每個請求漏 64KB（heap 256MB）：

- 第 7 分鐘 ✅ `jvm.old_gen_after_gc_high` 觸發（老年代 GC 後 100%）。
- ❌ `jvm.gc_overhead` 沒觸發：從「很滿」到 OutOfMemoryError 不到 1 分鐘，GC 拼命的階段太短。
- **之後 JVM 沒有結束**：每個執行緒（包括吐 metrics 的那個）都丟 OutOfMemoryError，但程式還在，Pod 顯示 `Running 2/2`、重啟 0 次。metrics 抓不到（`up = 0`），K8s 也不知道要重開。

這是真實環境常見的陷阱。**解法是 JVM 加 `-XX:+ExitOnOutOfMemoryError`**：OOM 時直接結束，K8s 重開，`container.restarted` 抓得到。沒加的話只剩 `scrape.down`（抓不到 metrics）看得出來。

第二次改成比較接近真實的速度（heap 128MB、每個請求 12KB；真實的洩漏通常是好幾小時到好幾天），並加上 `-XX:+ExitOnOutOfMemoryError`。

### 案例 2 第二次：老年代、major GC 次數、OOM 重開都抓到

老年代 GC 後的用量（`Tenured Gen`，上限 89MB）一路往上：

| 時間 | 開始洩漏後 | 老年代 GC 後 | 每分鐘 major GC | 每分鐘 GC 花的時間 |
|---|---|---|---|---|
| 07:09 | 0 | 21MB（24%） | 0 | 約 0.02 秒 |
| 07:13 | 4 分鐘 | 34MB（38%） | 0 | 0.01 秒 |
| 07:17 | 8 分鐘 | 57MB（64%） | 0 | 0.01 秒 |
| 07:21 | 12 分鐘 | 滿 | 2 | 0.25 秒 |
| 07:24 | 15 分鐘 | 滿 | **4** | 0.29 秒 |
| 07:25 | 16 分鐘 | 滿 | **8** | 0.53 秒 |
| 07:26 | 17 分鐘 | **OOM，JVM 結束，K8s 重開** | — | — |

加上 `jvm.major_gc_frequent` 之後，用腳本從頭再跑一次（`ONLY=2`），每分鐘跑一次規則：

| 規則 | 什麼時候觸發 | 比 OOM 早多久 |
|---|---|---|
| `jvm.old_gen_after_gc_high`（≥ 80%） | 第 13 分鐘（100%） | **約 4 分鐘** |
| `jvm.major_gc_frequent`（5 分鐘 ≥ 5 次） | 第 14 分鐘（5 次） | 約 3 分鐘 |
| `container.restarted` | 第 17 分鐘 | OOM 之後（加了 `-XX:+ExitOnOutOfMemoryError` 才有） |
| `jvm.gc_overhead`（≥ 10% 時間在 GC） | **沒觸發**：最多每分鐘 0.53 秒，不到 1% | — |

✅ 全部通過。

**為什麼「GC 時間比例」沒觸發：** heap 只有 128MB，一次 full GC 只要 70ms。比例要到 10%，一分鐘得 GC 6 秒，heap 這麼小根本做不到；heap 幾 GB 的正式服務一次 full GC 要好幾秒，這條才會先響。所以加了一條**看次數**的 `jvm.major_gc_frequent`：heap 大小都適用（平常幾分鐘 1 次，OOM 前每分鐘 4～8 次）。

**能提早多久，看洩漏多快：** 這裡 17 分鐘就漏光，最早的告警只提早 4 分鐘。真實的洩漏通常要好幾小時到好幾天，「老年代 GC 後 ≥ 80%」會提早幾十分鐘到幾小時。要再更早，就是之後的「趨勢」：從老年代 GC 後的斜率算出「幾小時後會滿」。

規則在記錄下來的真實資料上回測（`rules --at`）：07:20:30 都沒觸發、07:23:30 老年代觸發、07:25:45 老年代＋major GC 都觸發。

## 實測時發現的事

- **Java agent 的實際名稱**（2.15.0、2.20.0 相同）：
  - JVM：`jvm_memory_used_bytes`、`jvm_memory_used_after_last_gc_bytes`、`jvm_memory_limit_bytes`、`jvm_memory_committed_bytes`（維度 `jvm_memory_pool_name`、`jvm_memory_type`）、`jvm_gc_duration_seconds`（直方圖，維度 `jvm_gc_name`、`jvm_gc_action`）、`jvm_thread_count`、`jvm_cpu_recent_utilization_ratio`、`jvm_class_count`
  - 連線池：`db_client_connections_usage`（維度 `state`：used／idle）、`_max`、`_idle_min`、`_pending_requests`、`_wait_time_milliseconds`（直方圖）、`_use_time_milliseconds`、`_create_time_milliseconds`（維度 `pool_name`）
  - 是舊版的 `db_client_connections_*`，不是新版語意規範的 `db_client_connection_*`；之後升級 agent 或打開新語意規範時要跟著改規則。
  - 存在 ES 的 `metrics-generic.otel-<group>`。
- **JDK 內建的 HttpServer 沒有被 agent 記錄請求**（2.15、2.20 都沒有），所以測試程式沒有 `http.server.request.duration`。正式的服務用 Tomcat、Spring 等框架，agent 會記錄（延遲那條路之前用模擬的直方圖驗證過，見 [metrics.md](metrics.md)）。
- **OOM 之後 JVM 不會自己結束**：沒加 `-XX:+ExitOnOutOfMemoryError` 時，每個執行緒都丟 OutOfMemoryError，但程式還在，Pod 顯示正常、K8s 不會重開，metrics 也抓不到。**建議 app-common chart 預設加上 `-XX:+ExitOnOutOfMemoryError`**（加不加要決定：好處是 K8s 會重開、告警看得到；代價是本來可能「勉強撐著」的服務會直接重開）。要留 heap dump 再加 `-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=<掛載的路徑>`。
- **小容器的 JVM 用 Serial GC**：老年代叫 `Tenured Gen`。G1 是 `G1 Old Gen`、Parallel 是 `PS Old Gen`，規則用「名稱含 Old 或 Tenured」都抓得到。

## 還做不到的

| 項目 | 為什麼 | 要做什麼 |
|---|---|---|
| **p99、延遲跳階** | baseline 目前只學 p95 | `series.yaml` 加 p99（直方圖已經算得出來） |
| **網卡丟包**（`node_network_drop_total`） | 那是 node_exporter 的指標；kubelet 只有網路錯誤數 | 要部署 node_exporter，或接受只看錯誤數 |
| **長交易、慢查詢**（PostgreSQL、Oracle） | 要資料庫自己的 exporter | 依 DB 種類部署（postgres_exporter、oracledb_exporter） |
| **真的 Kafka** | 測試環境沒有 Kafka；名稱照 kafka_exporter | 正式環境部署 kafka_exporter 後確認名稱 |
| **機台指標** | EAP 還沒有 exporter | EAP 團隊照上面的名稱開；SECS/GEM 事件、FDC 製程參數要另外談 |
| **OEE** | 是「可用率 × 效能 × 良率」，要機台狀態時間、標準工時、良品數，來自 MES／EAP，不是現在收的 metrics | 由 MES／EAP 算好吐成指標，或從事件計算 |
| **慢慢爬**（洩漏要好幾天才滿） | 規則只看「現在多滿」；趨勢（幾天後會滿）還沒做 | 趨勢：算斜率、預測到上限的時間 |
| **自動處置**（重開、heap dump、擴容） | 目前只偵測、寫進 aiops-anomalies | 先做通知；自動處置要另外評估風險 |

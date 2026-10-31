# metrics 指標目錄：收了哪些、資料結構、哪些有 baseline

> **類型：規格 Reference**　同主題：[原理](README.md)・[規格（欄位）](reference.md)・[操作（部署、驗證）](how-to.md)・[metrics 能做什麼（原理）](capabilities.md)・[案例與實測](cases.md)・[.NET 原理](dotnet.md)・[.NET 操作](dotnet-how-to.md)・[.NET 規格](dotnet-reference.md)

一句話：**metrics 回答「有沒有出事、什麼時候開始、離極限多遠」。現在有：7 條 baseline（跟平常比）、狀態變化與離極限多遠的規則、Pod 之間互相比；還沒做的是「慢慢爬」（趨勢）。**

## 一、收了哪些（測試 ES 實際查到的欄位）

### 1. 基礎設施：kubelet（每個節點的 Collector 抓，30 秒一次）→ `metrics-kubeletstatsreceiver.otel-<group>`

| 對象 | 指標 | 意思 |
|---|---|---|
| 節點、Pod、容器 | `*.cpu.usage`、`*.cpu.time` | CPU 用量（核）、累計 CPU 時間 |
| | `*.memory.working_set`、`.rss`、`.usage`、`.available` | 記憶體。`working_set` 是 OOM 判斷用的那個 |
| | `*.memory.page_faults`、`.major_page_faults` | 缺頁；major 多代表在讀硬碟換頁 |
| | `*.filesystem.usage`、`.capacity`、`.available` | 磁碟用量與剩餘 |
| 節點、Pod | `*.network.io`、`*.network.errors` | 網路流量、錯誤封包 |
| 容器（kubelet 的 cAdvisor，只收這兩個） | `container_cpu_cfs_periods_total`、`container_cpu_cfs_throttled_periods_total` | CPU 週期數、其中被限流的；在 `metrics-prometheusreceiver.otel-<group>` |

### 2. K8s 物件狀態：k8s_cluster（整個 cluster 一個 Collector）→ `metrics-k8sclusterreceiver.otel-<group>`

| 對象 | 指標 | 意思 |
|---|---|---|
| 容器 | `k8s.container.restarts` | 重啟次數（累計） |
| | `k8s.container.ready` | 有沒有 ready |
| | `k8s.container.memory_limit`、`.memory_request`、`.cpu_request` | 設定的上限與保留量 |
| Pod | `k8s.pod.phase` | Pending／Running／Failed… |
| Deployment、ReplicaSet、StatefulSet、DaemonSet | `*.available`／`*.desired`、`*.ready_*` | 可用副本數 vs 要的副本數 |
| Job | `k8s.job.failed_pods`、`.successful_pods`… | 批次工作成功失敗 |
| 節點 | `k8s.node.condition_ready`、`_memory_pressure`、`_disk_pressure` | 節點狀態 |

### 3. 應用：Java 是 Prometheus 格式（Pod 標了 `prometheus.io/scrape`，Collector 30 秒抓一次）；.NET 用 OTLP 推（30 秒一次）

| 來源 | 指標 | 狀態 |
|---|---|---|
| **.NET 自動埋點**（chart `otel.dotnet`，不改程式，用 OTLP 推） | `http.server.request.duration`、`process.runtime.dotnet.*`（.NET 8）／`dotnet.*`（.NET 9）、`db.client.connections.*`（Npgsql） | ✅ 真的 ASP.NET Core 8、9 實測，見 [metrics/dotnet.md](dotnet.md) |
| Java agent（app-common chart 自動開） | `http_server_request_duration_seconds` → node Collector 改成 `http.server.request.duration`（直方圖：依 method、route、狀態碼；跟 .NET 同名） | ✅ 收到（測試應用模擬）→ `metrics-generic.otel-<group>` |
| | `http.client.request.duration`：往外呼叫 HTTP 的耗時 | ⚠️ 還沒驗證（測試程式沒有往外呼叫 HTTP） |
| | JVM：`jvm_memory_used_bytes`、`jvm_memory_limit_bytes`、`jvm_memory_used_after_last_gc_bytes`、`jvm_gc_duration_seconds`、`jvm_thread_count`、`jvm_cpu_recent_utilization_ratio` | ✅ 用真的 Java 程式＋agent 2.20.0 確認（[metrics/cases.md](cases.md)） |
| | 連線池（HikariCP）：`db_client_connections_usage{state}`、`_max`、`_pending_requests`、`_wait_time_milliseconds` | ✅ 同上（agent 2.x 用的是舊版名稱 `db_client_connections_*`） |
| 應用自己定義的 | 例如 `demo_requests_total`、業務量（每小時進站片數…） | 應用要自己開 |
| 抓取本身 | `up`、`scrape_duration_seconds` | `up = 0` 代表抓不到（服務掛了或 metrics port 壞了） |

## 資料結構（ES 裡長怎樣）

**一個資料流一個來源 × 一個 group**：`metrics-<來源>.otel-<group>`，例如 `metrics-kubeletstatsreceiver.otel-imc`。用 ES 的 time series data stream（TSDS，`index.mode: time_series`）存。

**一筆文件 = 某個時間點 × 某一條序列身分，裡面放所有「身分相同、單位相同」的指標。** 身分由資源欄位＋資料點欄位＋單位決定。所以一個 Pod 每 30 秒會變成十幾筆：

```
同一個 Pod、同一個時間點（@timestamp 1791607037047）
├─ unit {cpu}  attributes {}                                  → metrics: k8s.pod.cpu.usage
├─ unit s      attributes {}                                  → metrics: k8s.pod.cpu.time
├─ unit By     attributes {}                                  → metrics: k8s.pod.memory.working_set、.rss、.usage、.available、
│                                                                         k8s.pod.filesystem.usage、.capacity、.available
├─ unit By     attributes {interface: eth0, direction: receive}  → metrics: k8s.pod.network.io
├─ unit By     attributes {interface: eth0, direction: transmit} → metrics: k8s.pod.network.io
├─ unit 1      attributes {}                                  → metrics: k8s.pod.memory.page_faults、.major_page_faults
└─ …（容器那一層也各一組）
```

一筆實際的文件：

```json
{
  "@timestamp": 1791607037047,
  "start_timestamp": 1791601919000,            // 累計型的起算時間（Collector 開始抓的時間）
  "resource": { "attributes": {                 // 這是誰：Collector 補的 K8s 資訊與部門
      "k8s.cluster.name": "local", "k8s.namespace.name": "imc-wip-prod",
      "k8s.deployment.name": "http-metrics-app", "k8s.pod.name": "http-metrics-app-595b657c9-r9mv9",
      "k8s.pod.uid": "d8bd…", "k8s.node.name": "vm",
      "service.name": "http-metrics-app", "service.version": "1.0.0", "service.namespace": "demo",
      "dept": "D1110", "group": "imc" } },
  "scope": { "name": "…/receiver/kubeletstatsreceiver", "version": "0.162.0" },   // 誰產生的
  "attributes": {},                             // 這個數值自己的維度（例如網路的 direction、HTTP 的狀態碼）
  "unit": "{cpu}",
  "_metric_names_hash": "9e49df97",             // ES 用：這筆有哪些指標
  "metrics": { "k8s.pod.cpu.usage": 0.0011 },   // 數值本身
  "data_stream": { "type": "metrics", "dataset": "kubeletstatsreceiver.otel", "namespace": "imc" }
}
```

**欄位的型別（mapping）：**

| 欄位 | 型別 | 說明 |
|---|---|---|
| `resource.attributes.*`、`attributes.*` | `keyword`，**維度**（`time_series_dimension`） | 一組不同的值就是一條不同的序列。**不能放 `lot.id` 這種值很多的東西**，序列數會爆 |
| `metrics.*` 量測值（例如 `k8s.pod.cpu.usage`、`memory.working_set`、`deployment.available`） | `double`／`long`，**gauge** | 抓的那一刻的值，直接平均、取最大就好 |
| `metrics.*` 累計值（例如 `cpu.time`、`network.io`、`container.restarts`） | `double`／`long`，**counter** | 從開始一直累加；要「這 5 分鐘多少」得頭尾相減（或 ES|QL 的 `rate()`） |
| `metrics.http.server.request.duration` | `histogram`（`values`＋`counts`） | 每筆是**這 30 秒內**的請求耗時分布（Collector 換成差值）。可以直接加總次數、算 p95／p99；不存 `temporality`。總和另存在同一份文件的 `metrics.<名稱>_sum`（Collector 抽出來的，例如 `jvm_gc_duration_seconds_sum`） |
| `metrics.*` 的 `meta.unit` | — | 單位：`{cpu}` 核、`By` bytes、`s` 秒、`1` 個數 |

**其他設定：**

- `look_ahead_time: 2h`：ES 停機後補送的資料、時間略快的節點都收得進來（[metrics/README.md](README.md)）。
- 每一筆的 `_id` 由「序列身分＋時間」決定：同一條序列同一個時間點只會有一筆，重送不會重複。

**查詢時要注意：**

- **找某個指標要先過濾 `exists`**：同一個資料流裡混了很多種文件，例如查 Pod CPU 用 `{"exists": {"field": "metrics.k8s.pod.cpu.usage"}}`。
- **counter 不能直接平均**：要先算差值。baseline 目前只用 gauge 和直方圖，所以還沒處理；之後加「重啟次數」時要處理。
- **應用的直方圖在 `metrics-generic.otel-<group>`**，不在 `prometheusreceiver`：資料帶著應用自己的 scope 名稱。

## 二、哪些有 baseline（[engine/configs/baseline/series.yaml](../../engine/configs/baseline/series.yaml)）

| baseline | 來源 | 依什麼切 | 判斷 |
|---|---|---|---|
| `http.requests` | `http.server.request.duration` 的次數 | 服務 × method × API | 變多、變少都報 |
| `http.error_rate` | 同上，狀態碼 5xx 的比例 | 同上 | 變高 |
| `http.latency_p95_s` | 同上，p95 | 同上 | 變高 |
| `pod.cpu` | `k8s.pod.cpu.usage` 平均 | namespace × Deployment | 變高 |
| `pod.memory_bytes` | `k8s.pod.memory.working_set` 平均 | namespace × Deployment | 變高 |
| `log.lines`、`log.errors` | log 筆數（不是 metrics） | 服務 | — |

**其他全部沒有 baseline**：重啟、副本數、節點狀態、磁碟、網路、JVM、連線池、往外呼叫的耗時、p99。

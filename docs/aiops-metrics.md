# metrics：收了哪些、哪些有 baseline、metrics 能做到什麼

一句話：**metrics 回答「有沒有出事、什麼時候開始、離極限多遠」。現在收的指標夠多，但只有 7 條有 baseline；離極限多遠、慢慢爬、Pod 之間互相比、狀態類的規則都還沒做。**

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

### 3. 應用：Prometheus 格式（Pod 標了 `prometheus.io/scrape`，30 秒一次）

| 來源 | 指標 | 狀態 |
|---|---|---|
| **.NET 自動埋點**（chart `otel.dotnet`，不改程式，用 OTLP 推） | `http.server.request.duration`、`process.runtime.dotnet.*`（.NET 8）／`dotnet.*`（.NET 9）、`db.client.connections.*`（Npgsql） | ✅ 真的 ASP.NET Core 8、9 實測，見 [dotnet.md](dotnet.md) |
| Java agent（app-common chart 自動開） | `http.server.request.duration`（直方圖：依 method、route、狀態碼） | ✅ 收到（測試應用模擬）→ `metrics-generic.otel-<group>` |
| | `http.client.request.duration`：往外呼叫 HTTP 的耗時 | ⚠️ 還沒驗證（測試程式沒有往外呼叫 HTTP） |
| | JVM：`jvm_memory_used_bytes`、`jvm_memory_limit_bytes`、`jvm_memory_used_after_last_gc_bytes`、`jvm_gc_duration_seconds`、`jvm_thread_count`、`jvm_cpu_recent_utilization_ratio` | ✅ 用真的 Java 程式＋agent 2.20.0 確認（[aiops-metrics-cases.md](aiops-metrics-cases.md)） |
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
| `metrics.http_server_request_duration_seconds` | `exponential_histogram` | 每筆是**這 30 秒內**的請求耗時分布（Collector 換成差值，`temporality: delta`）。可以直接加總次數、算 p95／p99 |
| `metrics.*` 的 `meta.unit` | — | 單位：`{cpu}` 核、`By` bytes、`s` 秒、`1` 個數 |

**其他設定：**

- `look_ahead_time: 2h`：ES 停機後補送的資料、時間略快的節點都收得進來（[metrics.md](metrics.md)）。
- 每一筆的 `_id` 由「序列身分＋時間」決定：同一條序列同一個時間點只會有一筆，重送不會重複。

**查詢時要注意：**

- **找某個指標要先過濾 `exists`**：同一個資料流裡混了很多種文件，例如查 Pod CPU 用 `{"exists": {"field": "metrics.k8s.pod.cpu.usage"}}`。
- **counter 不能直接平均**：要先算差值。baseline 目前只用 gauge 和直方圖，所以還沒處理；之後加「重啟次數」時要處理。
- **應用的直方圖在 `metrics-generic.otel-<group>`**，不在 `prometheusreceiver`：資料帶著應用自己的 scope 名稱。

## 二、哪些有 baseline（[aiops/baseline/series.yaml](../aiops/baseline/series.yaml)）

| baseline | 來源 | 依什麼切 | 判斷 |
|---|---|---|---|
| `http.requests` | `http.server.request.duration` 的次數 | 服務 × method × API | 變多、變少都報 |
| `http.error_rate` | 同上，狀態碼 5xx 的比例 | 同上 | 變高 |
| `http.latency_p95_s` | 同上，p95 | 同上 | 變高 |
| `pod.cpu` | `k8s.pod.cpu.usage` 平均 | namespace × Deployment | 變高 |
| `pod.memory_bytes` | `k8s.pod.memory.working_set` 平均 | namespace × Deployment | 變高 |
| `log.lines`、`log.errors` | log 筆數（不是 metrics） | 服務 | — |

**其他全部沒有 baseline**：重啟、副本數、節點狀態、磁碟、網路、JVM、連線池、往外呼叫的耗時、p99。

## 三、metrics 能做到什麼

不是每一種問題都要「學平常」。依問題的形狀分五種做法：

| 做法 | 適合什麼 | 例子 | 要學嗎 | 現在 |
|---|---|---|---|---|
| **1. 跟平常比**（baseline） | 有週期、沒有固定標準的數字 | 請求數、延遲、錯誤率、CPU | 要 | ✅ 7 條 |
| **2. 離極限多遠**（飽和度） | 有上限的東西：滿了就出事 | 記憶體 ÷ 上限 > 90%、磁碟剩 < 10%、連線池使用中 ÷ 上限 > 90%、節點 disk/memory pressure | 不用：規則 | ✅ 容器記憶體、節點記憶體、節點磁碟（[rules.yaml](../aiops/baseline/rules.yaml)）；連線池、heap 待確認名稱 |
| **3. 慢慢爬**（趨勢） | 每天都在範圍內，但一直往上 | 連線池洩漏、heap 越來越高、磁碟越來越滿：算斜率，預測**幾天後到上限** | 不用學週期，算斜率 | ❌ |
| **4. 跟兄弟比**（同儕） | 同一個服務多個 Pod，一個壞掉被平均稀釋 | 只有 pod-7 錯誤率 100%、只有一個 Pod 記憶體一直漲 | 不用：當下比 | ❌ |
| **5. 狀態變化**（事件） | 發生了就是問題 | 重啟次數增加、可用副本 < 要的副本、Pod Failed、節點 NotReady、`up = 0`、Job 失敗 | 不用：規則 | ✅ 全部（k3s 實測：5 種壞法都抓到、正常的不誤報） |

另外兩種用法：

- **看尾巴**：p95 看不到千分之一的請求慢 5 秒（DNS 案例）。直方圖可以算 **p99** 和「超過 N 秒的比例」，再用做法 1 學。
- **對時間**（根因分析用）：異常那一刻，同一個服務的 GC 暫停、連線池等待、CPU 有沒有一起跳。GC 卡 6 秒、連線池滿這種，延遲和 runtime 指標同時變，就能指出原因。

### metrics 只看得到現象嗎？

**不只。** metrics 分兩種：

| | 看什麼 | 例子 | 回答 |
|---|---|---|---|
| **現象指標** | 服務對外的表現（請求數、錯誤率、延遲） | API 變慢、5xx 變多 | 「出事了、從幾點開始」 |
| **原因指標** | 服務靠的資源和狀態（用量、離上限多遠、事件） | 連線池滿了、GC 卡 6 秒、磁碟 0%、OOM 重啟、換版 | 「哪一種原因、在哪個 Pod」 |

現象指標一跳，同一時間看原因指標有沒有一起跳，就能指到原因的**種類**：

| 現象 | 同時跳的原因指標 | 指到的原因 |
|---|---|---|
| 延遲變高 | 連線池使用中 = 上限、等待連線變多 | 連線池耗盡（再往下是洩漏還是量太大） |
| 每 15 分鐘卡 6 秒 | `jvm.gc.duration` 同一時間 6 秒 | Full GC |
| 全部請求逾時 | 節點磁碟剩 0% | 磁碟滿（log 寫不出去卡住） |
| 錯誤變多 | 重啟次數增加、記憶體到上限 | OOM |
| 錯誤變多 | `service.version` 剛換 | 換版造成 |
| 只有 10% 錯 | 依 Pod 分開，只有 pod-7 | 單一 Pod 的問題 |

**metrics 到不了的，是更細的「為什麼」：**

```
出事了         ← metrics（現象）
哪個服務、哪個 Pod   ← metrics（依維度切開、跟兄弟比）
哪一種原因      ← metrics（原因指標對時間）：資源滿、GC、OOM、換版
哪一段、呼叫幾次   ← trace：N+1、重試、慢在哪個下游
哪一句、哪一筆    ← trace 屬性、log：哪句 SQL、哪個 lot.id、錯誤訊息
哪一行程式      ← log 的堆疊、人看程式碼
```

例如連線池洩漏：metrics 看得到「連線池滿了」，還能用趨勢預測「4 天後會滿」。但「哪個例外路徑忘了 close」要靠 trace 和 log，最後是人看程式。

**前提：原因指標要收得到。** 現在收了資源（CPU、記憶體、磁碟）和 K8s 狀態（重啟、副本、節點），還沒有 JVM、連線池；這兩類是 Java 服務最常見的原因（GC、連線池），要先確認 agent 的指標名稱。

### 對應 15 個案例（[aiops-cases.md](aiops-cases.md)）

| 案例 | 用 metrics 怎麼抓 | 做法 |
|---|---|---|
| 3 連線池洩漏 | 使用中 ÷ 上限一路往上 | 2＋3 |
| 5 Full GC | `jvm.gc.duration` 跟延遲 p99 同時跳；heap 鋸齒往上 | 1（p99）＋3＋對時間 |
| 11 DNS 偶發 5 秒 | p99、超過 5 秒的比例 | 1（尾巴） |
| 12 磁碟滿 | 節點或 Pod 磁碟剩餘 | 2＋3 |
| 14 訊息積壓後 OOM | 記憶體 ÷ 上限、重啟次數 | 2＋5 |
| 15 只有一個 Pod 壞 | 依 Pod 比錯誤率 | 4 |
| 8 執行緒池 | 執行緒數（JVM 有）；佇列長度要應用自己開 | 2 |

### metrics 做不到的

| 做不到 | 為什麼 | 要靠 |
|---|---|---|
| 慢在請求裡的哪一段、呼叫了幾次 | metrics 只有整體的數字 | trace（[trace 每一段的 baseline](../aiops/baseline/README.md#trace-每一段的-baseline)） |
| 錯誤被吞掉、回 200 | 狀態碼是 200，metrics 全綠 | log 樣式、業務量指標 |
| 哪一批貨、哪一台機台 | 不能用 `lot.id` 這種值當維度（每個值一條序列，ES 會爆） | trace 的屬性 |
| 30 秒內的短暫尖峰 | 30 秒才抓一次；直方圖記得住每個請求的耗時，但 CPU、記憶體這種只看得到抓的那一刻 | 直方圖、trace |

## 四、建議順序（待討論）

1. **狀態變化與飽和度（做法 5、2）**：✅ 已做，見 [aiops/baseline「規則」](../aiops/baseline/README.md#規則狀態變化離極限多遠)。
2. **跟兄弟比（做法 4）**：同樣不用歷史，抓單一 Pod 壞掉。
3. **補 baseline**：p99、超過 N 秒的比例、往外呼叫的耗時（`http.client.request.duration`）、重啟次數。
4. **確認 Java agent 的 JVM、連線池指標名稱**，再加飽和度與趨勢（做法 2、3）。
5. **趨勢（做法 3）**：要有幾天以上的資料才算得出斜率。

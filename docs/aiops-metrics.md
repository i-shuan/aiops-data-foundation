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
| Java agent（app-common chart 自動開） | `http.server.request.duration`（直方圖：依 method、route、狀態碼） | ✅ 收到（測試應用模擬）→ `metrics-generic.otel-<group>` |
| | `http.client.request.duration`：往外呼叫 HTTP 的耗時 | ⚠️ agent 會開，測試環境沒有真的 Java 應用，**還沒看到** |
| | JVM：`jvm.memory.used`、`jvm.memory.limit`、`jvm.memory.used_after_last_gc`、`jvm.gc.duration`、`jvm.thread.count`、`jvm.cpu.recent_utilization` | ⚠️ 同上，名稱依 agent 版本，要拿正式環境一個服務的 `/metrics` 確認 |
| | 連線池（HikariCP）：使用中、閒置、上限、等待中的請求、等待時間 | ⚠️ 同上；舊版名稱 `db.client.connections.*`、新版 `db.client.connection.*` |
| 應用自己定義的 | 例如 `demo_requests_total`、業務量（每小時進站片數…） | 應用要自己開 |
| 抓取本身 | `up`、`scrape_duration_seconds` | `up = 0` 代表抓不到（服務掛了或 metrics port 壞了） |

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
| **2. 離極限多遠**（飽和度） | 有上限的東西：滿了就出事 | 記憶體 ÷ 上限 > 90%、磁碟剩 < 10%、連線池使用中 ÷ 上限 > 90%、節點 disk/memory pressure | 不用：規則 | ❌ |
| **3. 慢慢爬**（趨勢） | 每天都在範圍內，但一直往上 | 連線池洩漏、heap 越來越高、磁碟越來越滿：算斜率，預測**幾天後到上限** | 不用學週期，算斜率 | ❌ |
| **4. 跟兄弟比**（同儕） | 同一個服務多個 Pod，一個壞掉被平均稀釋 | 只有 pod-7 錯誤率 100%、只有一個 Pod 記憶體一直漲 | 不用：當下比 | ❌ |
| **5. 狀態變化**（事件） | 發生了就是問題 | 重啟次數增加、可用副本 < 要的副本、Pod Failed、節點 NotReady、`up = 0`、Job 失敗 | 不用：規則 | ❌ |

另外兩種用法：

- **看尾巴**：p95 看不到千分之一的請求慢 5 秒（DNS 案例）。直方圖可以算 **p99** 和「超過 N 秒的比例」，再用做法 1 學。
- **對時間**（根因分析用）：異常那一刻，同一個服務的 GC 暫停、連線池等待、CPU 有沒有一起跳。GC 卡 6 秒、連線池滿這種，延遲和 runtime 指標同時變，就能指出原因。

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

1. **狀態變化與飽和度（做法 5、2）**：不用學、不用歷史，裝上去第一天就有用；規則簡單，誤報少。
2. **跟兄弟比（做法 4）**：同樣不用歷史，抓單一 Pod 壞掉。
3. **補 baseline**：p99、超過 N 秒的比例、往外呼叫的耗時（`http.client.request.duration`）、重啟次數。
4. **確認 Java agent 的 JVM、連線池指標名稱**，再加飽和度與趨勢（做法 2、3）。
5. **趨勢（做法 3）**：要有幾天以上的資料才算得出斜率。

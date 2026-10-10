# 應用是 .NET 時

一句話：**平台端（Collector、ES、baseline、trace baseline、根因分析）都不用換；.NET 服務在 chart 開 `otel.dotnet`，metrics 改用 OTLP 推，log 照 .NET 內建的 JSON 格式輸出就好。** 規則多了 .NET 自己的幾條（Gen2 GC、OutOfMemoryException、執行緒池飢餓）。

實測：真的 ASP.NET Core 9 ＋ OpenTelemetry .NET 自動埋點 v1.12.0 ＋ PostgreSQL（Npgsql），腳本 [test/local-k3s/run-dotnet-e2e.sh](../test/local-k3s/run-dotnet-e2e.sh)，結果見最後。

## 跟 Java 哪裡一樣、哪裡不一樣

| | Java | .NET |
|---|---|---|
| 怎麼掛 | chart `otel.javaAgent`：Java agent | chart `otel.dotnet`：OpenTelemetry .NET 自動埋點（CLR profiler，不改程式） |
| **metrics 怎麼送** | 應用開 Prometheus 格式，Collector 抓 | **用 OTLP 推給同節點的 Collector**（實測：.NET 自動埋點的 Prometheus 端點只聽 localhost，Collector 抓不到） |
| 部門怎麼判斷 | 依 Pod（Collector 抓的對象） | 依連線的來源 IP 是哪個 Pod（跟 trace 一樣），應用自己填的 K8s、部門欄位會被拿掉 |
| trace | agent 自動 | 自動：ASP.NET Core（Server）、Npgsql／SqlClient（Client）、HttpClient |
| log | logback JSON（`trace_id`） | .NET 內建 JSON console（`AddJsonConsole`，trace_id 在 `Scopes` 裡）或 Serilog compact JSON；**中央 pipeline 兩種都認得** |
| HTTP 指標 | `http_server_request_duration_seconds` | `http.server.request.duration`（**同一個指標**，OTLP 保留原名）；**預設沒開，chart 會加** |

## chart

```yaml
otel:
  javaAgent:
    enabled: false
  dotnet:
    enabled: true
    image: <內部 registry>/open-telemetry/opentelemetry-operator/autoinstrumentation-dotnet:1.12.0
    arch: linux-x64          # Alpine（musl）的 image 用 linux-musl-x64
```

chart 會：
- initContainer 把自動埋點複製進 Pod，設定 CLR profiler 的環境變數。
- metrics 用 OTLP 推到 `otel-node-collector.otel-collector.svc:4318/v1/metrics`，每 30 秒（跟 Collector 抓 Java 一樣）；不加 `prometheus.io` 標註、沒有 metrics port。
- 打開 ASP.NET Core 的 HTTP 指標：`OTEL_DOTNET_AUTO_METRICS_ADDITIONAL_SOURCES=Microsoft.AspNetCore.Hosting,Microsoft.AspNetCore.Server.Kestrel`（實測不加就沒有 `http.server.request.duration`）。
- `Logging__LogLevel__Microsoft.AspNetCore=Warning`：ASP.NET Core 預設**每個請求印 5 行 Information**（實測），log 量會暴增。

程式裡只要一件事：log 用 JSON 輸出、帶 scope（trace_id 在 scope 裡）：

```csharp
builder.Logging.AddJsonConsole(o => { o.IncludeScopes = true; o.UseUtcTimestamp = true; });
```

## Collector

node Collector 加了一條 `metrics/otlp`：OTLP 收進來的 metrics → 拿掉應用自填的 K8s、部門欄位 → 依連線來源 IP 補上 Pod、部門 → 直方圖換成差值 → 依部門送進 `metrics-generic.otel-<group>`（[node.yaml](../platform/cluster-addons/otel-collector/node.yaml)）。

## 實測的指標名稱（.NET 9、自動埋點 v1.12.0）

| 類 | 指標（ES 欄位是 `metrics.<名稱>`） | 維度 |
|---|---|---|
| HTTP | `http.server.request.duration`（直方圖）、`http.server.active_requests` | `http.request.method`、`http.route`、`http.response.status_code`（**數字**） |
| GC | `dotnet.gc.collections`（累計次數）、`dotnet.gc.last_collection.heap.size`、`dotnet.gc.pause.time`、`dotnet.gc.heap.total_allocated` | `gc.heap.generation`（gen0、gen1、gen2、loh、poh） |
| 執行緒池 | `dotnet.thread_pool.queue.length`、`dotnet.thread_pool.thread.count`、`dotnet.thread_pool.work_item.count` | — |
| 其他 runtime | `dotnet.exceptions`（依 `error.type`）、`dotnet.monitor.lock_contentions`、`dotnet.process.memory.working_set`、`dotnet.process.cpu.time` | |
| 連線池（Npgsql） | `db.client.connections.usage`、`db.client.connections.max`、`db.client.connections.create_time`、`db.client.commands.duration` | `pool.name`、`state`（idle／used） |
| 往外 HTTP | `http.client.request.duration`、`http.client.active_requests` | `server.address`、`http.response.status_code` |

**沒有的：** heap 的上限（.NET 在容器裡 heap 上限預設是容器記憶體的 75%，沒有指標），連線池排隊數（Npgsql 沒有 `pending_requests`）。

## baseline 與規則

- **baseline 的 `http.*` 序列不用分語言**：[series.yaml](../aiops/baseline/series.yaml) 每條有兩種寫法（`variants`：Java 的欄位名稱、OTLP 的欄位名稱），結果用統一的 keys（`service`、`method`、`route`）。
- **.NET 的規則**（[rules.yaml](../aiops/baseline/rules.yaml)）：

| 規則 | 看什麼 | 對應 Java 的 |
|---|---|---|
| `dotnet.gen2_gc_frequent` | 5 分鐘內 Gen2 GC ≥ 5 次 | `jvm.major_gc_frequent` |
| `dotnet.out_of_memory` | 5 分鐘內有 `System.OutOfMemoryException` | （Java 看老年代 ÷ 上限；.NET 沒有上限的指標） |
| `dotnet.thread_pool_growth` | 執行緒 ÷ CPU 核數 ≥ 16，2 分鐘 | Java 沒有標準指標 |
| `dotnet.thread_pool_starvation` | 執行緒池排隊 ≥ 10，2 分鐘（.NET 偵測不到的阻塞：lock、`Thread.Sleep`） | 同上 |
| `db.pool_exhausted_otlp` | 連線池使用中 ÷ 上限 ≥ 90% | `db.pool_exhausted` |

容器層的規則（重啟、記憶體快到上限、CPU 限流…）跟語言無關，照用。

## 實測結果

`run-dotnet-e2e.sh`，2026-10-10，k3s 單節點，最後一次全部通過；應用：ASP.NET Core 9（1 核、記憶體上限 512Mi）＋ PostgreSQL，Npgsql 連線池上限 5，每秒約 15 個請求。

### 資料

| 檢查 | 結果 |
|---|---|
| HTTP、GC、執行緒池、連線池的指標用 OTLP 進 `metrics-generic.otel-imc` | ✅ |
| 部門依連線來源判斷（應用沒填 K8s、部門欄位，仍是 IMC／D1110） | ✅ |
| baseline 的 `http.requests`、`http.error_rate`、`http.latency_p95_s` 跟 Java 同一條序列，算得出 .NET 的值 | ✅ |
| .NET log 拆得出 level（`Information` → `info`）、logger（`Category`）、訊息；結構化參數（例如 `OrderId`）留在 `app.State` | ✅ |
| .NET log 的 `trace_id`（在 `Scopes` 裡）對得到 trace：每一筆都對到 2 個 span（ASP.NET Core 的請求＋Npgsql 的查詢） | ✅ |
| 正常時 .NET 的規則都不報 | ✅ |

### 案例

| 案例 | 怎麼做 | 抓到的 | 結果 |
|---|---|---|---|
| 連線池用光 | 拿了連線不還 | `db.pool_exhausted_otlp`：使用中 **100%** | ✅ |
| 執行緒池飢餓 | 每個請求 `Task.Delay(2000).Wait()`，同時請求變 5 倍 | `dotnet.thread_pool_growth`：執行緒 **6 → 44 條**（1 核）；p95 2.5ms → 1.75 秒 | ✅（見下） |
| 記憶體洩漏 | 每個請求 64KB 放進不會清的 list | `dotnet.gen2_gc_frequent` 第 7 分鐘；到底時見下。`container.memory_near_limit` **不一定觸發**（四次裡一次） | ✅ |

### 實測時發現的事

**1. 執行緒池飢餓時「佇列」不會排，是「執行緒」暴增。** 第一版規則看佇列長度，結果佇列一直是 0：.NET 6 以後偵測到「同步等 Task」（`.Wait()`、`.Result`）會馬上多開執行緒。所以症狀是執行緒 6 → 44 條、每條卡 2 秒、延遲變長。改成看「執行緒 ÷ CPU 核數」（`dotnet.thread_pool_growth`）；佇列那條留給 .NET 偵測不到的阻塞（lock、`Thread.Sleep`）。

**2. 記憶體洩漏到底有兩種結局，同一支程式、同樣設定，跑四次各出現兩次：**

| | 結局一（第 1、4 次） | 結局二（第 2、3 次） |
|---|---|---|
| 結局 | 工作集到 522MB（上限 512Mi），**被容器的 OOM 砍掉、K8s 重開** | GC heap 到上限（380MB，容器的 75%）後**程式不結束**：每個請求丟 OutOfMemoryException、回 500，Gen2 GC 不停跑（2 分鐘 6,500 次） |
| 抓到的 | `container.restarted` | `dotnet.out_of_memory`（OutOfMemoryException 1,503 次）、`dotnet.gen2_gc_frequent`；HTTP 錯誤率 baseline 也會報 |

第二種跟 Java 沒加 `-XX:+ExitOnOutOfMemoryError` 一樣：Pod 看起來正常、K8s 不會重開。.NET 沒有對應的開關；要在程式裡處理（例如 OutOfMemoryException 時結束程式），或靠上面的規則通知人。

**3. 容器記憶體的規則對 .NET 不可靠。** .NET 在容器裡 GC heap 的上限是容器記憶體的 75%，heap 滿了之後工作集停在 90% 上下：四次測試只有一次超過 `container.memory_near_limit` 的 90%。.NET 的洩漏要看 `dotnet.gen2_gc_frequent`，到底時看 `dotnet.out_of_memory` 或 `container.restarted`。

**4. `error.type` 是短名稱**：.NET 寫 `OutOfMemoryException`，不是 `System.OutOfMemoryException`（第一版規則因此沒觸發，在記錄下來的資料上回測確認改對）。

**5. Npgsql 連線池的名稱是連線字串**（密碼會拿掉），告警會顯示 `Host=…;Username=…;Maximum Pool Size=5`。建議程式用 `NpgsqlDataSourceBuilder` 設 `Name`，例如 `orders-db`。

**6. 已經刪掉的 Pod 不該一直報。** 計數類的規則（Gen2 次數、OutOfMemoryException）原本對「已經刪掉的 Pod」也照算，刪掉後 10 分鐘內還會報。改成跟其他規則一樣：最新的資料超過 2 分鐘就不報；只有容器重啟、Job 失敗照報（`report_gone`）。

**7. 測試環境才有的：** NuGet 經過會重簽憑證的 proxy，編譯時要信任它的 CA（[dotnet-app.yaml](../test/local-k3s/dotnet-app.yaml)）。正式環境 image 先建好，不在 Pod 裡編譯。

### 還沒驗證

- **SQL Server（Microsoft.Data.SqlClient）的連線池指標**：這次用 PostgreSQL（Npgsql）。SqlClient 的指標名稱、有沒有連線池指標要另外確認。
- **Serilog**：中央 pipeline 認得 compact JSON 的欄位（`@m`、`@l`、`@x`、`@tr`），但沒有用真的 Serilog 程式跑過。
- **.NET 8**：這次是 .NET 9。.NET 8 的 runtime 指標名稱可能不同（`System.Runtime` meter 是 .NET 9 才內建）。

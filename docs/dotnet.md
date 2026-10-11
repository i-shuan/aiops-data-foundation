# 應用是 .NET 時

一句話：**不改程式。** chart 開 `otel.dotnet`，掛 OpenTelemetry .NET 自動埋點（跟 Java agent 一樣的做法）；metrics、trace 都由應用推給同節點的 Collector（OTLP），log 的格式也用環境變數設定。平台端（ES、baseline、trace baseline、根因分析）不用換，規則多了 .NET 自己的幾條（Gen2 GC、例外、執行緒池）。

實測：真的 ASP.NET Core 8（當作幾年前留下來、不能改的舊專案）＋ 自動埋點 v1.12.0 ＋ PostgreSQL（Npgsql），腳本 [test/local-k3s/run-dotnet-e2e.sh](../test/local-k3s/run-dotnet-e2e.sh)，結果見後面。之前也用 .NET 9 跑過一輪，全部通過。

## 為什麼選「自動埋點＋推」

公司的 .NET 服務很多是幾年前留下來的舊專案，分散在不同部門，要其他團隊改程式很難推。所以選**維運自己就能做完、不用動程式**的做法。

| | A. 自動埋點＋推（**採用**） | B. 程式裡加 SDK＋拉 |
|---|---|---|
| 改程式 | **不用** | 要加 8 個套件、約 10 行設定 |
| metrics | 應用每 30 秒推給同節點 Collector | Collector 每 30 秒來抓 `/metrics` |
| 指標名稱 | 點分隔（`http.server.request.duration`），跟 Java 不同，baseline、規則有兩種寫法 | 跟 Java 同名 |
| 部門判斷 | 看連線來源 IP 是哪個 Pod（跟 trace 一樣） | 看 Pod 的 namespace |
| 實測 | .NET 8、.NET 9 都過 | .NET 8 過（見最後「選項 B」） |

**為什麼不能「自動埋點＋拉」：** 自動埋點的 Prometheus 端點只聽 localhost（實測），Collector 抓不到。要拉就得在程式裡加 SDK，也就是 B。

**推會不會壓垮 Collector：** 每個 Pod 每 30 秒送一批（`OTEL_METRIC_EXPORT_INTERVAL=30000`），資料量跟被抓一次差不多。差別只是節奏由應用決定、Collector 忙不過來時只能回「429 等一下」。幾百個 Pod 的規模兩種都不是問題。

**trace 本來就只能推：** trace 是一筆一筆的事件，要等人來拉就得一直暫存在應用裡，所以 OTLP 本身只有推（gRPC `Export`、HTTP `POST /v1/traces`）。Java、.NET 都一樣：請求結束後先放進記憶體佇列，批次送到同節點 Collector 的 4318。佇列有上限（預設 2048 筆），Collector 連不上時滿了會**丟**，不會把應用的記憶體撐爆，但也不是零遺漏。

| | Java | .NET |
|---|---|---|
| metrics | Collector 抓 `:9464/metrics` | 應用推 `POST :4318/v1/metrics` |
| trace | 應用推 `POST :4318/v1/traces` | 同左 |
| log | Collector 讀節點上的 `/var/log/pods` | 同左 |

**不支援的：** .NET Framework（4.x）只能跑在 Windows／IIS，上不了 K8s Linux；要先移植到 .NET 6 以上。

## 跟 Java 哪裡一樣、哪裡不一樣

| | Java | .NET |
|---|---|---|
| 怎麼掛 | chart `otel.javaAgent`：Java agent | chart `otel.dotnet`：自動埋點（CLR profiler），**都不改程式** |
| 部門怎麼判斷 | 依 Pod 的 namespace | 依連線的來源 IP 是哪個 Pod（跟 trace 一樣）；應用自己填的 K8s、部門欄位會被拿掉 |
| trace | agent 自動 | 自動：ASP.NET Core（Server）、Npgsql／SqlClient（Client）、HttpClient |
| log | logback JSON（`trace_id`） | .NET 內建 JSON console（chart 用環境變數打開，trace_id 在 `Scopes` 裡）或 Serilog compact JSON；**中央 pipeline 兩種都認得** |
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
- metrics 用 OTLP 推到 `otel-node-collector.otel-collector.svc:4318/v1/metrics`，每 30 秒；不加 `prometheus.io` 標註、沒有 metrics port。
- 打開 ASP.NET Core 的 HTTP 指標：`OTEL_DOTNET_AUTO_METRICS_ADDITIONAL_SOURCES=Microsoft.AspNetCore.Hosting,Microsoft.AspNetCore.Server.Kestrel`（實測不加就沒有 `http.server.request.duration`）。
- `Logging__LogLevel__Microsoft.AspNetCore=Warning`：ASP.NET Core 預設**每個請求印 5 行 Information**（實測），log 量會暴增。
- **log 用 JSON、帶 scope**，全部用環境變數，程式不用改：

```yaml
Logging__Console__FormatterName: json
Logging__Console__FormatterOptions__IncludeScopes: "true"      # trace_id、span_id 在 scope 裡
Logging__Console__FormatterOptions__UseUtcTimestamp: "true"
Logging__Console__FormatterOptions__TimestampFormat: yyyy-MM-ddTHH:mm:ss.fffZ
```

> 程式自己用 Serilog、NLog 寫 log 的話，上面這組沒作用：Serilog 改成 compact JSON（`CompactJsonFormatter`）輸出到 console，中央 pipeline 認得；NLog 還沒驗證。程式自己 `ClearProviders()` 後只加文字格式的，也要改設定檔。

## Collector

node Collector 有一條 `metrics/otlp`：OTLP 收進來的 metrics → 拿掉應用自填的 K8s、部門欄位 → 依連線來源 IP 補上 Pod、部門 → 直方圖換成差值 → 依部門送進 `metrics-generic.otel-<group>`（[node.yaml](../platform/cluster-addons/otel-collector/node.yaml)）。

## 實測的指標名稱（自動埋點 v1.12.0，ES 欄位是 `metrics.<名稱>`）

HTTP、連線池兩版同名；**runtime 的名稱 .NET 8 跟 .NET 9 完全不同**。

| 類 | .NET 8（以前） | .NET 9（以後） | 維度 |
|---|---|---|---|
| HTTP | `http.server.request.duration`（直方圖）、`http.server.active_requests` | 同左 | `http.request.method`、`http.route`、`http.response.status_code`（**數字**） |
| GC 次數 | `process.runtime.dotnet.gc.collections.count` | `dotnet.gc.collections` | .NET 8：`generation`；.NET 9：`gc.heap.generation`（gen0、gen1、gen2…） |
| GC 其他 | `process.runtime.dotnet.gc.heap.size`、`…gc.duration`、`…gc.allocations.size` | `dotnet.gc.last_collection.heap.size`、`dotnet.gc.pause.time` | |
| 執行緒池 | `process.runtime.dotnet.thread_pool.threads.count`、`…queue.length` | `dotnet.thread_pool.thread.count`、`…queue.length` | |
| 例外 | `process.runtime.dotnet.exceptions.count`（**沒分種類**） | `dotnet.exceptions`（依 `error.type`） | |
| CPU 核數 | `process.cpu.count` | `dotnet.process.cpu.count` | |
| 連線池（Npgsql） | `db.client.connections.usage`、`db.client.connections.max`、`db.client.commands.duration` | 同左 | `pool.name`、`state`（idle／used） |
| 往外 HTTP | `http.client.request.duration` | 同左 | `server.address`、`http.response.status_code` |

**沒有的：** heap 的上限（.NET 在容器裡 heap 上限預設是容器記憶體的 75%，沒有指標），連線池排隊數（OTLP 這版沒有 `pending_requests`）。

## baseline 與規則

- **baseline 的 `http.*` 序列不用分語言**：[series.yaml](../aiops/baseline/series.yaml) 每條有兩種寫法（`variants`：Java 的欄位名稱、OTLP 的欄位名稱），結果用統一的 keys（`service`、`method`、`route`）。
- **.NET 的規則**（[rules.yaml](../aiops/baseline/rules.yaml)）：.NET 8 以前用 `dotnet8.*`、.NET 9 以後用 `dotnet.*`，一個服務只會有其中一種名稱，不會重複報。

| 規則（.NET 8 ／ .NET 9） | 看什麼 | 對應 Java 的 |
|---|---|---|
| `dotnet8.gen2_gc_frequent` ／ `dotnet.gen2_gc_frequent` | 5 分鐘內 Gen2 GC ≥ 5 次 | `jvm.major_gc_frequent` |
| `dotnet8.exceptions_burst` ／ `dotnet.out_of_memory` | .NET 8：5 分鐘內例外 ≥ 500 次（分不出種類）；.NET 9：有 `OutOfMemoryException` | — |
| `dotnet8.thread_pool_growth` ／ `dotnet.thread_pool_growth` | 執行緒 ÷ CPU 核數 ≥ 16，2 分鐘 | Java 沒有標準指標 |
| `dotnet8.thread_pool_starvation` ／ `dotnet.thread_pool_starvation` | 執行緒池排隊 ≥ 10，2 分鐘（.NET 偵測不到的阻塞：lock、`Thread.Sleep`） | 同上 |
| `db.pool_exhausted_otlp` | 連線池使用中 ÷ 上限 ≥ 90% | `db.pool_exhausted` |

容器層的規則（重啟、記憶體快到上限、CPU 限流…）跟語言無關，照用。

## 踩過的坑（實測時發現的事）

### 收資料

**1. 自動埋點的 Prometheus 端點只聽 localhost。** Collector 抓不到，所以 metrics 改成推（見「為什麼選自動埋點＋推」）。

**2. 自動埋點預設沒有 ASP.NET Core 的 HTTP 指標。** 要加 `OTEL_DOTNET_AUTO_METRICS_ADDITIONAL_SOURCES=Microsoft.AspNetCore.Hosting,…`，不加就沒有 `http.server.request.duration`（chart 已經加）。

**3. ASP.NET Core 預設每個請求印 5 行 Information。** log 量會暴增，chart 設 `Logging__LogLevel__Microsoft.AspNetCore=Warning`。

**4. .NET JSON log 的 trace_id 不在最外層，在 `Scopes` 裡。** 要 `IncludeScopes`；中央 pipeline 改成會從 `Scopes` 拿 `TraceId`、`SpanId`（[pipeline-fields.painless](../platform/central/elasticsearch/pipeline-fields.painless)），也認得 Serilog compact JSON。JSON 格式、scope 都能用環境變數打開（`Logging__Console__*`），舊專案不用改程式。

**5. Npgsql 連線池的名稱是連線字串**（密碼會拿掉），告警會顯示 `Host=…;Username=…;Maximum Pool Size=5`。舊專案不改程式就只能這樣；能改的話用 `NpgsqlDataSourceBuilder` 設 `Name`。

### 規則

**6. 執行緒池飢餓時「佇列」不會排，是「執行緒」暴增。** 第一版規則看佇列長度，結果一直是 0：.NET 6 以後偵測到「同步等 Task」（`.Wait()`、`.Result`）會馬上多開執行緒。症狀是執行緒 6 → 40 幾條（1 核）、每條卡 2 秒、延遲變長，所以看「執行緒 ÷ CPU 核數」（`thread_pool_growth`）；佇列那條留給 .NET 偵測不到的阻塞（lock、`Thread.Sleep`）。

**7. 記憶體洩漏到底有兩種結局，容器記憶體的規則對 .NET 不可靠。** 同一支程式、同樣設定，.NET 9 跑四次各出現兩次：

| | 結局一 | 結局二 |
|---|---|---|
| 結局 | 工作集到上限，**被容器的 OOM 砍掉、K8s 重開** | GC heap 到上限（容器的 75%）後**程式不結束**：每個請求丟 OutOfMemoryException、回 500，Gen2 GC 不停跑 |
| 抓到的 | `container.restarted` | `dotnet.out_of_memory`（.NET 9）／`dotnet8.exceptions_burst`（.NET 8）、Gen2 GC；HTTP 錯誤率 baseline 也會報 |

結局二跟 Java 沒加 `-XX:+ExitOnOutOfMemoryError` 一樣：Pod 看起來正常、K8s 不會重開。.NET 沒有對應的開關，只能靠上面的規則通知人。
.NET 在容器裡 heap 上限是容器記憶體的 75%，heap 滿了之後工作集停在 90% 上下：四次只有一次超過 `container.memory_near_limit` 的 90%。.NET 的洩漏要看 Gen2 GC 的規則。

**8. 例外的種類：.NET 9 是短名稱，.NET 8 根本沒有。** .NET 9 的 `error.type` 寫 `OutOfMemoryException`，不是 `System.OutOfMemoryException`（第一版規則因此沒觸發）。.NET 8 只有例外總數，所以 .NET 8 的規則看「例外暴增」（5 分鐘 ≥ 500 次）。

**9. 已經刪掉的 Pod 不該一直報。** 計數類的規則（Gen2 次數、例外次數）原本對已刪掉的 Pod 照算，刪掉後 10 分鐘內還會報。改成跟其他規則一樣：最新的資料超過 2 分鐘就不報；只有容器重啟、Job 失敗照報（`report_gone`）。

**10. .NET 8 跟 .NET 9 的 runtime 指標名稱完全不同。** .NET 9 內建 `System.Runtime` meter（`dotnet.*`）；.NET 8 以前是 `process.runtime.dotnet.*`，維度也不一樣（`generation` vs `gc.heap.generation`）。所以 .NET 的規則有兩套（`dotnet8.*`、`dotnet.*`）。HTTP、連線池兩版同名。

**11. 測試環境才有的：** NuGet 經過會重簽憑證的 proxy，編譯時要信任它的 CA（[dotnet-app.yaml](../test/local-k3s/dotnet-app.yaml)）；正式環境 image 先建好，不在 Pod 裡編譯。

## Kafka（Confluent.Kafka）

自動埋點支援 Confluent.Kafka 1.4～2.x，不改程式時（k3s 實測，[docs/aiops-trace.md 場景四](aiops-trace.md#四非同步訊息佇列)）：

- **生產端**：HTTP 請求 → `orders publish` 在同一個 trace。
- **消費端**：`orders receive` 是**新的 trace**，只用 link 指回生產端；它的時間是 `Consume()` 等下一筆訊息等了多久，不是處理時間。
- **消費端處理時查的 DB、呼叫的 API 每一筆都自成一個 trace**，對不回是哪一筆訊息。

平台的做法：排隊時間從 link 找生產端算；沒有上一層的 DB span 當成自己的入口學 baseline（看得到消費者寫 DB 變慢、出錯）；積壓看 kafka_exporter 的 lag。要整條串起來得在消費端改程式（約 5 行，用 header 的 `traceparent` 開處理的 span），目前不做。

## 實測結果

`run-dotnet-e2e.sh`，2026-10-10，k3s 單節點，全部通過。應用：ASP.NET Core 8（**程式沒有任何 OpenTelemetry、log 設定**，1 核、記憶體上限 512Mi）＋ PostgreSQL，Npgsql 連線池上限 5，每秒約 15 個請求。之前 .NET 9 跑的那輪也全部通過（洩漏跑四次，兩種結局各兩次，見坑 7）。

### 資料

| 檢查 | 結果 |
|---|---|
| HTTP、GC、執行緒池、連線池的指標用 OTLP 進 `metrics-generic.otel-imc`（.NET 8 的 `process.runtime.dotnet.*`） | ✅ |
| 部門依連線來源判斷（應用沒填 K8s、部門欄位，仍是 IMC／D1110） | ✅ |
| baseline 的 `http.requests`、`http.error_rate`、`http.latency_p95_s` 跟 Java 同一條序列，算得出 .NET 的值（每 5 分鐘約 1,250 個請求、p95 2.5ms） | ✅ |
| log 只靠環境變數就是 JSON：拆得出 level（`Information` → `info`）、logger（`Category`）、訊息 | ✅ |
| log 的 `trace_id`（在 `Scopes` 裡）對得到 trace（ASP.NET Core 的請求＋Npgsql 的查詢） | ✅ |
| 正常時 .NET 的規則（.NET 8、9 兩套）都不報 | ✅ |

### 案例

| 案例 | 怎麼做 | 抓到的 | 結果 |
|---|---|---|---|
| 連線池用光 | 拿了連線不還 | `db.pool_exhausted_otlp`：使用中 **100%**（池的名稱是連線字串，見坑 5） | ✅ |
| 執行緒池飢餓 | 每個請求 `Task.Delay(2000).Wait()`，同時請求變 5 倍 | `dotnet8.thread_pool_growth`：執行緒 **44 條／核**；佇列最多 1 個（再次確認佇列不會排，見坑 6） | ✅ |
| 記憶體洩漏 | 每個請求 64KB 放進不會清的 list | 第 7 分鐘 `dotnet8.gen2_gc_frequent`（5 分鐘 1,399 次 Gen2）＋ `dotnet8.exceptions_burst`（2,098 次）：程式沒結束、一直丟例外（結局二）；`container.memory_near_limit` 沒觸發 | ✅ |

### 還沒驗證

- **SQL Server（Microsoft.Data.SqlClient）的連線池指標**：這次用 PostgreSQL（Npgsql）。SqlClient 的指標名稱、有沒有連線池指標要另外確認。
- **.NET 6**：自動埋點支援，名稱應該跟 .NET 8 一樣（`process.runtime.dotnet.*`），沒實測。
- **Serilog、NLog**：中央 pipeline 認得 Serilog compact JSON（`@m`、`@l`、`@x`、`@tr`），但沒用真的 Serilog 程式跑過；NLog 沒看過。
- **Alpine（musl）image**：chart 有 `arch: linux-musl-x64`，沒實測。

## 選項 B：程式裡加 SDK＋拉（能改程式的新專案可以考慮）

能改程式的新專案也可以在程式裡加 OpenTelemetry SDK，開 `/metrics` 讓 Collector 抓，好處是指標名稱跟 Java 一樣（`http_server_request_duration_seconds`、`db_client_connections_*`）、部門照 namespace。目前平台**沒有**為它準備 chart 模式和規則（採用的是 A）；要用的話先討論。

2026-10-10 實測過一輪（ASP.NET Core 8 ＋ SDK 1.19，程式在 git 歷史 `4a36110` 的 `test/local-k3s/dotnet-app/`）：資料、log 對 trace、正常不報、連線池用光、執行緒暴增（44 條）、洩漏（Gen2 第 7 分鐘、到底一直丟例外）全部通過。踩到的：

- 要加 8 個套件，其中 `OpenTelemetry.Exporter.Prometheus.AspNetCore` 一直是 beta、`Instrumentation.Process` 是 rc。
- Collector 每次來抓也算一個請求（`http_route="/metrics"`），baseline 要排除；`/metrics` 只開在 9464（`RequireHost("*:9464")`）。
- **`AddOtlpExporter()` 不認 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`／`_PROTOCOL`**（Java、自動埋點用的那組）：trace 照預設送 `localhost:4317`，連不上一直重試、**每次重試都丟例外**（正常時 5 分鐘 574 次），讓例外規則誤報。要給通用的 `OTEL_EXPORTER_OTLP_ENDPOINT`、`OTEL_EXPORTER_OTLP_PROTOCOL`。找原因的方法：程式目錄放 `OTEL_DIAGNOSTICS.json`（自我診斷 log）。
- `OpenTelemetry.Exporter.OpenTelemetryProtocol` 1.9.0 有已知弱點警告，要用 1.19.x。

# 應用是 .NET 時

一句話：**平台端（Collector、ES、baseline、trace baseline、根因分析）都不用換，.NET 跟 Java 一樣：應用在 9464 開 `/metrics`、Collector 來抓（pull）。** .NET 這邊要在程式裡設定 OpenTelemetry SDK（十行左右）、chart 開 `otel.dotnet`；log 用 .NET 內建的 JSON 格式。規則多了 .NET 自己的幾條（Gen2 GC、例外暴增、執行緒池飢餓）。

實測：真的 ASP.NET Core 8 ＋ OpenTelemetry .NET SDK 1.19 ＋ PostgreSQL（Npgsql），腳本 [test/local-k3s/run-dotnet-e2e.sh](../test/local-k3s/run-dotnet-e2e.sh)，結果見最後。

## 為什麼是 pull、不用自動埋點

- **全部用 pull**：Collector 照同一個節奏（30 秒）去抓每個應用，速度由平台控制。用 push 的話每個應用各自決定什麼時候送，應用一多、或一起重開後同時補送，壓力全落在 Collector 上，Collector 也擋不住。
- 先試過**自動埋點**（CLR profiler，不改程式）：實測它的 Prometheus 端點**只聽 localhost**，Collector 抓不到，只能 push。改成**在程式裡設定 SDK**，`/metrics` 就能讓 Collector 抓，跟 Java 一樣。
- **只有 metrics 用 pull，trace 還是推**：metrics 是「現在的數字」，什麼時候來抓都可以；trace 是一筆一筆的事件，要等人來拉就得一直暫存在應用裡，所以 OTLP 本身只有推（gRPC `Export`、HTTP `POST /v1/traces`）。Java、.NET 都一樣：請求結束後 SDK 先放進記憶體佇列，批次送到同節點 Collector 的 4318。佇列有上限（.NET、Java 預設 2048 筆），Collector 連不上時滿了會**丟**，不會把應用的記憶體撐爆，但也不是零遺漏。log 則是兩者都不是：Collector 直接讀節點上的容器 log 檔。

| | 誰發起 | 怎麼送 |
|---|---|---|
| metrics | Collector 每 30 秒來抓 | `GET :9464/metrics` |
| trace | 應用推 | OTLP/HTTP `POST :4318/v1/traces` |
| log | Collector 讀檔 | 節點上的 `/var/log/pods` |

- 附帶的好處：指標名稱跟 Java 一樣（`http_server_request_duration_seconds`、`db_client_connections_*`），**baseline 的序列、連線池的規則跟 Java 共用**。

## 跟 Java 哪裡一樣、哪裡不一樣

| | Java | .NET |
|---|---|---|
| 怎麼掛 | chart `otel.javaAgent`：Java agent，不改程式 | 程式裡設定 OpenTelemetry SDK（下面的程式碼）＋ chart `otel.dotnet` |
| metrics | 開在 9464 的 `/metrics`，Collector 抓 | **一樣** |
| 部門怎麼判斷 | 依 Pod 的 namespace | **一樣** |
| trace | agent 自動送 Collector | SDK 送 Collector：ASP.NET Core（Server）、Npgsql（Client）、HttpClient |
| log | logback JSON（`trace_id`） | .NET 內建 JSON console（`AddJsonConsole`，trace_id 在 `Scopes` 裡）或 Serilog compact JSON；**中央 pipeline 兩種都認得** |
| HTTP 指標 | `http_server_request_duration_seconds` | **同名** |
| 連線池 | HikariCP：`db_client_connections_usage`、`_max`、`_pending_requests` | Npgsql：**同名**（`usage`、`max`、`pending_requests` 都有） |

## 程式裡要加的（平台要求的設定）

套件（[app.csproj](../test/local-k3s/dotnet-app/app.csproj)）：

| 套件 | 版本 | 用途 |
|---|---|---|
| `OpenTelemetry.Extensions.Hosting` | 1.19.1 | SDK |
| `OpenTelemetry.Exporter.Prometheus.AspNetCore` | 1.19.1-**beta**.1 | 開 `/metrics` |
| `OpenTelemetry.Exporter.OpenTelemetryProtocol` | 1.19.1 | trace 送 Collector |
| `OpenTelemetry.Instrumentation.AspNetCore` | 1.19.0 | HTTP 指標、trace |
| `OpenTelemetry.Instrumentation.Http` | 1.19.0 | 往外的 HTTP trace |
| `OpenTelemetry.Instrumentation.Runtime` | 1.19.0 | GC、執行緒池、例外 |
| `OpenTelemetry.Instrumentation.Process` | 1.19.0-**rc**.1 | 行程記憶體、CPU 時間 |
| `Npgsql.OpenTelemetry` | 跟 Npgsql 同版 | 資料庫 trace；連線池指標是 Npgsql 內建的 meter |

> Prometheus exporter 上游一直是 beta（只有這一個），公司能不能用 beta 套件要確認。

`Program.cs`（[完整範例](../test/local-k3s/dotnet-app/Program.cs)）：

```csharp
builder.Logging.ClearProviders();
builder.Logging.AddJsonConsole(o => { o.IncludeScopes = true; o.UseUtcTimestamp = true; });   // trace_id 在 scope 裡

builder.Services.AddOpenTelemetry()
    .WithMetrics(m => m
        .AddAspNetCoreInstrumentation()     // http.server.request.duration
        .AddRuntimeInstrumentation()        // GC、執行緒池、例外
        .AddProcessInstrumentation()
        .AddMeter("Npgsql")                 // 連線池（用 SqlClient 的話換掉，見「還沒驗證」）
        .AddPrometheusExporter())
    .WithTracing(t => t
        .AddAspNetCoreInstrumentation()
        .AddHttpClientInstrumentation()
        .AddNpgsql()
        .AddOtlpExporter());                // 位址、協定讀 chart 給的 OTEL_EXPORTER_OTLP_TRACES_*
var app = builder.Build();
app.MapPrometheusScrapingEndpoint().RequireHost("*:9464");   // /metrics 只開在 9464，業務的 port 沒有
app.Urls.Add("http://0.0.0.0:8080");
app.Urls.Add("http://0.0.0.0:9464");
```

服務名稱、trace 位址、取樣都由 chart 給的 `OTEL_*` 環境變數決定，程式不用寫死。連線池記得取名字（`NpgsqlDataSourceBuilder.Name`），不然告警裡的名稱是整串連線字串。

## chart

```yaml
otel:
  javaAgent:
    enabled: false
  dotnet:
    enabled: true
```

chart 會：
- 加 `prometheus.io/*` 標註、metrics port（9464）、`OTEL_*` 環境變數，跟 Java 一樣；trace 位址另外給通用的 `OTEL_EXPORTER_OTLP_ENDPOINT`／`_PROTOCOL`（.NET 不認 `_TRACES_` 那組，見坑 12）。
- `Logging__LogLevel__Microsoft.AspNetCore=Warning`：ASP.NET Core 預設**每個請求印 5 行 Information**（實測），log 量會暴增。
- `javaAgent` 跟 `dotnet` 只能開一個。

## 實測的指標名稱（.NET 8、SDK 1.19，ES 欄位是 `metrics.<名稱>`）

| 類 | 指標 | 維度 |
|---|---|---|
| HTTP | `http_server_request_duration_seconds`（直方圖）、`http_server_active_requests` | `http_request_method`、`http_route`、`http_response_status_code` |
| GC | `process_runtime_dotnet_gc_collections_count_total`、`process_runtime_dotnet_gc_heap_size_bytes`、`process_runtime_dotnet_gc_allocations_size_bytes_total` | `generation`（gen0、gen1、gen2、loh、poh） |
| 執行緒池 | `process_runtime_dotnet_thread_pool_threads_count`、`process_runtime_dotnet_thread_pool_queue_length`、`process_runtime_dotnet_thread_pool_completed_items_count_total` | — |
| 例外 | `process_runtime_dotnet_exceptions_count_total` | **沒有分種類**（.NET 9 才有 `error.type`） |
| GC 暫停、lock | `process_runtime_dotnet_gc_duration_nanoseconds_total`、`process_runtime_dotnet_monitor_lock_contention_count_total` | |
| 行程 | `process_memory_usage_bytes`、`process_cpu_time_seconds_total`、`process_thread_count` | |
| Kestrel | `kestrel_active_connections`、`kestrel_queued_connections` | |
| 連線池（Npgsql） | `db_client_connections_usage`、`db_client_connections_max`、`db_client_connections_pending_requests`、`db_client_commands_*` | `pool_name`、`state`（idle／used） |

**沒有的：** heap 的上限（.NET 在容器裡 heap 上限預設是容器記憶體的 75%，沒有指標）、CPU 核數、例外的種類。

> Collector 每次來抓也會被算成一個請求（`http_route="/metrics"`），baseline 的 `http.*` 序列把這個路徑排除。

## baseline 與規則

- **The baseline's `http.*` series are shared with Java** ([series.yaml](../aiops/baseline/series.yaml)), and the connection-pool rules `db.pool_exhausted` and `db.pool_waiting` are shared too.
- **.NET rules** ([rules.yaml](../aiops/baseline/rules.yaml)):

| 規則 | 看什麼 | 對應 Java 的 |
|---|---|---|
| `dotnet.gen2_gc_frequent` | 5 分鐘內 Gen2 GC ≥ 5 次 | `jvm.major_gc_frequent` |
| `dotnet.exceptions_burst` | 5 分鐘內例外 ≥ 500 次（.NET 8 分不出種類） | — |
| `dotnet.thread_pool_growth` | 執行緒池的執行緒 ≥ 32 條，2 分鐘 | Java 沒有標準指標 |
| `dotnet.thread_pool_starvation` | 執行緒池排隊 ≥ 10，2 分鐘（.NET 偵測不到的阻塞：lock、`Thread.Sleep`） | 同上 |

容器層的規則（重啟、記憶體快到上限、CPU 限流…）跟語言無關，照用。

## 踩過的坑（實測時發現的事）

1–9 是第一輪（.NET 9 ＋ 自動埋點 ＋ OTLP 推）遇到的，10–13 是改成 .NET 8 ＋ SDK ＋ pull 之後遇到的；除了有註明的，結論改完後一樣適用。

### 收資料

**1. .NET 自動埋點的 Prometheus 端點只聽 localhost。** Collector 抓不到，所以一度改成 OTLP 推；後來改成在程式裡設定 SDK，`/metrics` 就抓得到了（見「為什麼是 pull」）。

**2. 自動埋點預設沒有 ASP.NET Core 的 HTTP 指標。** 要加 `OTEL_DOTNET_AUTO_METRICS_ADDITIONAL_SOURCES=Microsoft.AspNetCore.Hosting,…`，不加就沒有 `http.server.request.duration`。改用 SDK 後 `AddAspNetCoreInstrumentation()` 就有，不再需要。

**3. ASP.NET Core 預設每個請求印 5 行 Information。** log 量會暴增，chart 設 `Logging__LogLevel__Microsoft.AspNetCore=Warning`。

**4. .NET JSON log 的 trace_id 不在最外層，在 `Scopes` 裡。** 要 `IncludeScopes = true`；中央 pipeline 改成會從 `Scopes` 拿 `TraceId`、`SpanId`（[pipeline-fields.painless](../platform/central/elasticsearch/pipeline-fields.painless)），也認得 Serilog compact JSON。

**5. Npgsql 連線池的名稱是連線字串**（密碼會拿掉），告警會顯示 `Host=…;Username=…;Maximum Pool Size=5`。程式用 `NpgsqlDataSourceBuilder` 設 `Name`（例如 `orders-db`）。

### 規則

**6. 執行緒池飢餓時「佇列」不會排，是「執行緒」暴增。** 第一版規則看佇列長度，結果一直是 0：.NET 6 以後偵測到「同步等 Task」（`.Wait()`、`.Result`）會馬上多開執行緒。症狀是執行緒 6 → 44 條（1 核）、每條卡 2 秒、延遲變長。所以加了 `dotnet.thread_pool_growth`；佇列那條留給 .NET 偵測不到的阻塞（lock、`Thread.Sleep`）。

**7. 記憶體洩漏到底有兩種結局，容器記憶體的規則對 .NET 不可靠。** 同一支程式、同樣設定，跑四次各出現兩次：

| | 結局一 | 結局二 |
|---|---|---|
| 結局 | 工作集到上限，**被容器的 OOM 砍掉、K8s 重開** | GC heap 到上限（容器的 75%）後**程式不結束**：每個請求丟 OutOfMemoryException、回 500，Gen2 GC 不停跑 |
| 抓到的 | `container.restarted` | `dotnet.exceptions_burst`、`dotnet.gen2_gc_frequent`；HTTP 錯誤率 baseline 也會報 |

結局二跟 Java 沒加 `-XX:+ExitOnOutOfMemoryError` 一樣：Pod 看起來正常、K8s 不會重開。.NET 沒有對應的開關，要在程式裡處理（例如 OutOfMemoryException 時結束程式），或靠上面的規則通知人。
.NET 在容器裡 heap 上限是容器記憶體的 75%，heap 滿了之後工作集停在 90% 上下：四次只有一次超過 `container.memory_near_limit` 的 90%。.NET 的洩漏要看 `dotnet.gen2_gc_frequent`。

**8. 例外的種類：.NET 9 是短名稱，.NET 8 根本沒有。** .NET 9 的 `error.type` 寫 `OutOfMemoryException`，不是 `System.OutOfMemoryException`（第一版規則因此沒觸發）。.NET 8（`OpenTelemetry.Instrumentation.Runtime`）只有例外總數，所以規則改成 `dotnet.exceptions_burst`（5 分鐘 ≥ 500 次）。

**9. 已經刪掉的 Pod 不該一直報。** 計數類的規則（Gen2 次數、例外次數）原本對已刪掉的 Pod 照算，刪掉後 10 分鐘內還會報。改成跟其他規則一樣：最新的資料超過 2 分鐘就不報；只有容器重啟、Job 失敗照報（`report_gone`）。

### .NET 8 ＋ pull 之後

**10. .NET 8 跟 .NET 9 的 runtime 指標名稱完全不同。** .NET 9 內建 `System.Runtime` meter（`dotnet.gc.collections`、`dotnet.thread_pool.thread.count`…）；.NET 8 要用 `OpenTelemetry.Instrumentation.Runtime`，名稱是 `process_runtime_dotnet_*`。而且沒有 CPU 核數，`dotnet.thread_pool_growth` 從「執行緒 ÷ 核數 ≥ 16」改成固定「≥ 32」，核數多的服務要調高。HTTP、連線池的名稱兩版都跟 Java 一樣。

**11. Collector 來抓也會被算成請求**（`http_route="/metrics"`，每 30 秒一次），會拉低 p95、多算流量；baseline 的 `http.*` 序列排除這個路徑。`/metrics` 只開在 9464（`RequireHost("*:9464")`），業務的 8080 抓 `/metrics` 是 404（e2e 有檢查）。

**12. `AddOtlpExporter()` 不認 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`／`_PROTOCOL`（Java 用的那組）。** 第一次跑 trace 一筆都沒進 ES：.NET 照預設送 `localhost:4317`（gRPC），連不上就一直重試，**每次重試都丟例外**（正常時 5 分鐘 574 次），連帶讓 `dotnet.exceptions_burst` 在正常時誤報、洩漏案例的「例外暴增」也不能算數。chart 對 .NET 改給通用的 `OTEL_EXPORTER_OTLP_ENDPOINT`（`:4318`，它自己接 `/v1/traces`）和 `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`；改完 trace 進得來、正常時例外不再增加。取樣比例（`OTEL_TRACES_SAMPLER_ARG`）.NET 有認（實測設 0.1，trace 數掉到一成左右）。找原因的方法：在程式目錄放 `OTEL_DIAGNOSTICS.json`（OpenTelemetry .NET 的自我診斷 log）。

**13. 測試環境才有的：** NuGet 經過會重簽憑證的 proxy，編譯時要信任它的 CA（[dotnet-app.yaml](../test/local-k3s/dotnet-app.yaml)），正式環境 image 先建好。另外 `OpenTelemetry.Exporter.OpenTelemetryProtocol` 1.9.0 有已知弱點警告，用 1.19.x。

## 實測結果

RESULTS_PLACEHOLDER

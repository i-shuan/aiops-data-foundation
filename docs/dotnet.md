# 應用是 .NET 時

一句話：**平台端（Collector、ES、baseline、trace baseline、根因分析）都不用換，.NET 跟 Java 一樣：應用在 9464 開 `/metrics`、Collector 來抓（pull）。** .NET 這邊要在程式裡設定 OpenTelemetry SDK（十行左右）、chart 開 `otel.dotnet`；log 用 .NET 內建的 JSON 格式。規則多了 .NET 自己的幾條（Gen2 GC、例外暴增、執行緒池飢餓）。

實測：真的 ASP.NET Core 8 ＋ OpenTelemetry .NET SDK 1.19 ＋ PostgreSQL（Npgsql），腳本 [test/local-k3s/run-dotnet-e2e.sh](../test/local-k3s/run-dotnet-e2e.sh)，結果見最後。

## 為什麼是 pull、不用自動埋點

- **全部用 pull**：Collector 照同一個節奏（30 秒）去抓每個應用，速度由平台控制。用 push 的話每個應用各自決定什麼時候送，應用一多、或一起重開後同時補送，壓力全落在 Collector 上，Collector 也擋不住。
- 先試過**自動埋點**（CLR profiler，不改程式）：實測它的 Prometheus 端點**只聽 localhost**，Collector 抓不到，只能 push。改成**在程式裡設定 SDK**，`/metrics` 就能讓 Collector 抓，跟 Java 一樣。
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
- 加 `prometheus.io/*` 標註、metrics port（9464）、`OTEL_*` 環境變數，跟 Java 一樣。
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

## 實測結果

RESULTS_PLACEHOLDER

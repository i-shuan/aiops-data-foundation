# 收值、共同鑰匙圈、log↔trace 綁定、metrics 時間窗：這週的驗證（2026-10）

**狀態**：完成（docker：pipeline 案例、Collector → ES 的交叉查詢、metrics 三層窗都驗過；補了 OTLP log 的路和 pipeline 兩個漏洞；**真的 Java agent、.NET 自動埋點各跑一次，純文字 log 也綁得起來**）　　**日期**：2026-10-27　　**相關**：分支 `claude/ingest-check`；對照簡報「PDCA (Phase 1) 收值端整合、共同鑰匙圈與極限壓測」；[field-unify](2026-10-field-unify.md)、[trace-durability](2026-10-trace-durability.md)、[docs/rca/README「共同鑰匙圈」](../rca/README.md#共同鑰匙圈完整清單)
**模組**：① 收值、② 清資料、③ 存

## Plan：這一次要達到什麼

- **需求**：簡報把 Phase 1 寫成「已完成（k3s 單節點驗證通過）」，Check 四列都是「持續觀察中」。要回答三個問題，而且是**這週能重跑的**驗證，不是引用以前的紀錄：
  1. **收值、共同鑰匙圈**：三種資料進 ES 之後，用同一組鑰匙（服務、Pod、namespace、cluster、時間、`trace_id`）真的查得到、對得起來嗎？
  2. **SDK 用自動注入（Java agent、.NET 自動埋點），log 跟 trace 能綁嗎？** 綁得起來的條件是什麼、什麼情況會綁不起來？
  3. **metrics 的聚合是用時間窗嗎？** 從 Collector 到 ES 到 baseline，每一層的「窗」是什麼、對不對得上？沒有就補。
- **目的**：
  - 三個問題各有一個**在 docker 就能跑**的驗證（這台沒有 k3s），結果寫進 Check；跟 k3s 的紀錄互相對照。
  - 問題 2 的答案寫成一張表（哪種 log 輸出、trace_id 在哪、pipeline 認不認得），綁不起來的情況給做法。
  - 問題 3 如果缺哪一層就補程式；不缺就把三層的窗寫清楚。
- **範圍**：只做收值到存（①②③）＋ baseline 讀 ES 的那一段窗。不做壓測（簡報的 5,000 logs/sec 要 k3s）、不做 Kafka 串接。

### 先盤點（動手前，從程式和文件看到的）

| 問題 | 現況 | 證據 |
|---|---|---|
| 共同鑰匙圈 | 三種資料同一條路徑：`resource.attributes.service.name`、`resource.attributes.k8s.{namespace,pod,cluster}.name`、`trace_id`、`span_id`；log 是 alias 指到 ECS 欄位（`service.name`、`kubernetes.*`、`trace.id`） | `telemetry/storage/component-mappings.json`、[field-unify](2026-10-field-unify.md)；k3s trace e2e 23 項 |
| log↔trace（Java agent） | agent 把 `trace_id`、`span_id` 放進 MDC；**要 log 的輸出格式有印 MDC** 才會在 stdout 上。SDK 的 `logback-spring.xml`（LogstashEncoder）會印；pipeline 認 `trace_id`／`traceId`／`trace.id` | `sdk/standards/java/logback-spring.xml`、`pipeline-fields.painless` |
| log↔trace（.NET 自動埋點） | chart 用環境變數開 JSON console＋`IncludeScopes`，`TraceId` 在 `Scopes` 裡；pipeline 會從 `Scopes` 拿；Serilog compact 的 `@tr` 也認 | `_deployment.tpl`、`pipeline-fields.painless`、[dotnet.md](../metrics/dotnet.md) |
| log↔trace 綁不起來的情況 | 應用用自己的純文字 pattern（沒印 MDC）：stdout 沒有 trace_id，pipeline 無從解析。chart 設 `OTEL_LOGS_EXPORTER=none`，agent 不會另外送 log；node Collector 的 `logs/in` 只收 filelog，**沒有 OTLP log 的 pipeline** | `_deployment.tpl` 第 102 行、`node.yaml` pipelines |
| metrics 時間窗 | 三層：(1) Collector 每 30 秒抓一次，`cumulativetodelta` 把累加值換成「這 30 秒的量」；(2) ES 存 30 秒一筆（TSDS）；(3) baseline 用 `date_histogram` 依序列的粒度（30 秒～1 小時，自己選）加總，p95 用直方圖的 percentile | `node.yaml`、`series.yaml`、`detector/es.py` |

### 要做的

1. **驗證 A（docker）**：ES pipeline `_simulate`：logback JSON、.NET console JSON（Scopes）、Serilog compact、純文字四種 log 各一行，看 `trace.id`、`span.id`、`service.name` 有沒有；加成 `e2e/pipeline/run-trace-cases.sh`（跟 level、timestamp 的案例測試同一種寫法，以後可以一直跑）。
2. **驗證 B（docker）**：起 Collector（contrib 0.162.0）接 OTLP，送一個 trace（兩段）＋一筆同 `trace_id` 的 OTLP log＋同服務的 metrics 到 ES（寫入 key）；用鑰匙圈的路徑交叉查：trace → log（`trace_id`）、log → trace、metrics → trace（服務＋時間）。
3. **實作：OTLP log 的路**：node Collector 加 `logs/otlp` pipeline（otlp receiver → 同一套 processors → ES）；chart 的 `OTEL_LOGS_EXPORTER` 可以設 `otlp`。這樣應用的 log pattern 不印 MDC 時，agent 仍會把帶 trace context 的 log 直接送出來，綁得起來。驗證 B 的 log 就走這條。
4. **驗證 C（docker）**：假 Java 的 Prometheus 直方圖（每 5 秒 20 個成功＋3 個 500）→ Collector 每 10 秒抓、換增量 → ES；再用 baseline 的 `fetch` 以 1 分鐘的窗加總，看請求數、錯誤率、p95 對不對（請求數 ≈ 276／分、錯誤率 ≈ 13%、p95 > 2.5 秒）。
5. 文件：問題 2 的表寫進 `docs/trace/README.md`「跟 log 串起來」；簡報的 Check 對照寫進本 PDCA。
6. **驗證 D（docker，真的 agent）**：不是自己模擬 OTLP，而是真的掛 agent：Tomcat 10 的 JSP 用 `java.util.logging` 印純文字（掛 Java agent 2.10.0），ASP.NET Core 8 用內建 `ILogger` 的預設純文字 console（掛 .NET 自動埋點 1.12.0），兩個都設 `OTEL_LOGS_EXPORTER=otlp`、程式裡沒有任何 OpenTelemetry 的東西、log 格式沒印 trace 欄位。看 ES 裡的 log 有沒有 trace_id、對不對得到 trace 裡的 Server span。
7. **.NET Framework 吃不吃得進去**：查官方文件，寫清楚哪些 signal 行、哪些不行。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `e2e/pipeline/trace-cases.json`、`run-trace-cases.sh`（新） | log↔trace 的案例測試：7 種 log 輸出（logback JSON、camelCase JSON、ECS JSON、.NET console＋Scopes、Serilog compact、純文字沒印 MDC、純文字有印 MDC）丟 `k8s-logs` 的 `_simulate`，比對 `trace.id`、`span.id`、`log.level`；第二、三段用 `k8s-logs-otel` 模擬 agent 直送的 log record 和 span 的例外事件 | 問題 2 的答案要能一直重跑 |
| `telemetry/collectors/node.yaml` | 新 pipeline `logs/otlp`：otlp receiver → `transform/logs-otlp-pre`（跟 trace 一樣拿掉自帶的 K8s、部門欄位，擋冒充）→ `k8s_attributes` → `transform/group` → ES | 應用的 log pattern 不印 MDC 時，agent 直送的 log record 本身帶 trace_id，走這條就綁得起來；之前沒有這條路 |
| `sdk/charts/app-common`（`_deployment.tpl`、`_helpers.tpl`）、`sdk/examples/mes-api/values.yaml` | `otel.logs.exporter`：`none`（預設，stdout）或 `otlp`（多設 `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`、協定）；值不對 `helm template` 會失敗 | 讓應用團隊一個開關就能切 |
| `telemetry/storage/pipeline-otel.painless` | agent 直送的 log record（有 `trace_id` 或 `severity_text`）：把 `trace_id`、`span_id`、`severity_text`、自訂 attributes 帶成 `_otel`，再重組成 Fluent Bit 的形狀 | **驗證 B 抓到的漏洞**：這條 pipeline 只重組 body 和 K8s 身分，trace_id 被丟掉，OTLP 直送的 log 跟 trace 對不起來 |
| `telemetry/storage/pipeline-fields.painless` | 拆完原文後，原文裡沒有的從 `_otel` 補：`trace.id`、`span.id`、`log.level`、自訂欄位進 `app`；span 的例外事件變成的 log record（沒有 body）補 `message`、`level: error`、`error.type` | 同上；**另一個發現**：ES 的 OTLP trace 入口會把 span 的例外事件另外存成一筆 log（之前文件寫「不存」是指 span 文件裡沒有） |
| `docs/trace/README.md`、`docs/rca/README.md`、`sdk/standards/tracing.md` | 問題 2 的表（哪種輸出、trace_id 在哪、綁不綁得起來、要做什麼） | |

## Check：測試的目的與結果

全部在 docker：ES 9.5.5（`telemetry/scripts/elasticsearch-setup.sh` 裝的 pipeline、template、寫入 key）、otelcol-contrib 0.162.0（照 node.yaml 的處理器，`k8s_attributes` 補身分那段用 `resource` 處理器寫死）、假 Java（busybox 吐 Prometheus 直方圖：每 5 秒 20 個成功＋3 個 500，500 的在 2.5～5 秒那格）。沒有 K8s，所以 filelog 讀容器 log 檔、`k8s_attributes` 依 Pod 補身分、trace-sampler 尾端取樣這三段沒驗（k3s 的 e2e 有）。

| 問題 | 目的 | 怎麼測 | 結果 |
|---|---|---|---|
| 1 共同鑰匙圈 | 三種資料用同一組路徑查得到、對得起來 | Collector 送一個 trace（mes-api `GET /lots/{id}` → `SELECT mes.lots` 出錯，帶例外事件）＋同 trace_id 的 log＋metrics 到 ES；用 `fields` 取 `resource.attributes.service.name`、`k8s.{namespace,pod,cluster}.name`、`dept`、`group`、`trace_id`、`span_id` | trace 兩段、log 都在 `*-it` 的 data stream，鑰匙的值全部一樣（mes-api／mes-prod／mes-api-7d9f-abcde／c1／D3120／it）；應用自帶的假 namespace、`dept: D9999` 被拿掉；例外的 type 抄到 span 上 |
| 1 交叉查 | trace → log、log → trace、metrics → trace | 拿 SELECT 那段的 `span_id` 查 log；拿 log 的 `trace_id` 查 trace；拿 metrics 的服務＋時間查 trace | 都查得到：span_id → 那行「Connection pool timeout」；trace_id → 兩段（含例外 type）；服務＋時間 → 同一個 trace |
| 2 log↔trace，各種輸出格式 | 哪些綁得起來 | `run-trace-cases.sh` 7 案例 | 5 種 JSON 都拿到 `trace.id`、`span.id`、level；純文字兩種拿不到（預期，這就是要補 OTLP 那條路的原因） |
| 2 OTLP 直送的 log | 不靠 log 格式也綁得起來 | 同上第二段（`_simulate`）＋驗證 B 真的送一筆 | **第一次失敗**：`k8s-logs-otel` 把 trace_id 丟了（hits 0）。修 pipeline 後：`trace_id`、`span_id`、`log.level: error`、`app.lot_id`、`app.thread.name` 都在，service.name 從 pod label 補上 |
| 2 意外發現 | — | 依 trace_id 查 log 回了 2 筆 | 第二筆是 span 的例外事件：ES 的 OTLP trace 入口把它存成一筆 log record（`event.name: exception`，沒有 body）。補 message、level、error.type 後它變成一筆有用的 log（第三段案例） |
| 3 metrics 時間窗 | 三層的窗對得上 | 假 Java 每 5 秒＋23 個請求；Collector 每 10 秒抓、`cumulativetodelta`；baseline 的 `fetch` 用 1 分鐘的窗加總 | ES 每分鐘 12 筆（200、500 各 6 筆，每筆 `start_timestamp` 到 `@timestamp` 剛好 10 秒、count 6 = 3 × 2 個 5 秒）；`http.requests` **276／分**（= 23 × 12）、`http.error_rate` **13.04%**（= 36/276）、`http.latency_p95_s` **3.75 秒**（500 的都在 2.5～5 秒那格，p95 落在格內插值）；連續兩分鐘一模一樣 |
| Collector 設定還是合法的 | 加了 pipeline 沒寫壞 | `otelcol validate` common.yaml＋node.yaml（掛假的 serviceaccount token、自簽 CA） | 改前改後都過 |
| **D 真的 Java agent** | 舊專案的純文字 log，不改程式、不改 log 設定，綁不綁得起來 | `tomcat:10.1-jdk21` ＋ `JAVA_TOOL_OPTIONS=-javaagent:…`（chart 用的 autoinstrumentation-java 2.10.0 的 jar）＋ `OTEL_LOGS_EXPORTER=otlp`；JSP 用 `java.util.logging` 印「order N handled」、錯誤 | stdout 是 Tomcat 的純文字（`27-Oct-2026 07:13:58 INFO [http-nio-8080-exec-2] … order 2 handled`，沒有任何 trace 欄位）；ES 的 `k8s-it-c1` 裡同一行帶 `trace_id: ee5ab1…`、`span_id: bedd5a…`、`log.level`；拿這個 trace_id 去 traces 查到 `GET /index.jsp` 的 Server span，span_id 一樣 |
| **D 真的 .NET 自動埋點** | 同上，.NET | `mcr.microsoft.com/dotnet/aspnet:8.0` ＋ chart 同一組 `CORECLR_*`、`DOTNET_STARTUP_HOOKS`（autoinstrumentation-dotnet 1.12.0）＋ `OTEL_LOGS_EXPORTER=otlp`、`OTEL_DOTNET_AUTO_LOGS_INCLUDE_FORMATTED_MESSAGE=true`；程式只有 `ILogger.LogInformation`／`LogError`，console 是預設純文字（**沒有** chart 的 `Logging__Console__*` JSON、scope 設定） | stdout 純文字（`info: Program[0]` ＋ 下一行 `order 500 handled`）；ES 裡同一個請求 6 筆 log 都帶 `trace_id: aa67f9…`（應用的 2 筆＋ASP.NET Core 框架的 Executing／Executed／Request finished）；`LogError` 那筆 `log.level: error`，span_id 對到 `GET /orders/{id:int}` 的 Server span |

**對照簡報的 Check**：「關聯機制驗證：可透過共同鑰匙圈及 trace_id 正確串接」這次在 docker 重做一次是成立的，但**只對 log 格式有印 trace_id 的應用成立**；純文字 pattern 的舊專案要開 `otel.logs.exporter: otlp`，而且這條路在這次之前根本不通（pipeline 會丟 trace_id）。「收值效能 5,000 logs/sec、0 遺失」這次沒測（要 k3s）。

## Act：Check 後沒改善的、還沒解決的

- **.NET Framework（4.x）吃不吃得進去**（在什麼情況下發現：問「大部分是 C#」時）。查 OpenTelemetry .NET 自動埋點的官方文件（opentelemetry.io/docs/zero-code/dotnet、config.md，2026-10-27）：
  - **trace、metrics：可以**。「The minimal supported version of .NET Framework is 4.6.2」，Windows x64，IIS、Windows Service 都掛得上（CLR profiler）。
  - **log 直送（做法 2）：ILogger 那條不行**。文件明寫 ILogger 的 log instrumentation「Not supported on .NET Framework」，而且要 `Microsoft.Extensions.Logging` ≥ 8。.NET Framework 的舊專案多半也不是用 ILogger。
  - **log4net、NLog 有另一條**：文件列了 log4net（≥ 2.0.13）、NLog（≥ 5.0）的 bytecode instrumentation，「trace context injection and a logs bridge」，沒有標 Framework 不支援，但標 Experimental；**沒驗過**。
  - 所以 .NET Framework 的結論：trace、metrics 照 K8s 上 .NET 的做法（但要在 Windows VM 上跑，Framework 進不了 Linux 容器）；log 要看用什麼套件：log4net／NLog 5 以上可能走得通（要驗），自己寫檔的只能用服務＋時間對。
- **真的 agent 只在 docker 驗，不是 K8s**：身分（namespace、Pod、部門）是 Collector 設定寫死的；K8s 上由 `k8s_attributes` 依連線來源補，這一段還是要在 k3s 跑（`e2e/local-k3s` 的 java-app、dotnet-app 設 `otel.logs.exporter: otlp`）。
- **.NET 開 otlp 後框架的 log 也一起來**（一個請求 6 筆，4 筆是 ASP.NET Core 的 Executing／Executed／Request finished）：量會比 stdout 大很多。chart 對 .NET 已經把 `Logging__LogLevel__Microsoft.AspNetCore` 設成 Warning，走 otlp 的 log 一樣受這個設定管（這次測試沒設所以都進來了）；正式環境保持 Warning。
- **（2026-10-27 補：已在 k3d 驗，[k3d-e2e](2026-10-k3d-e2e.md)；`helm template` 過、`logs/otlp` 在 K8s 上 Java 通了，.NET 在 K8s 上沒跑）**
- **chart 的改動沒有 `helm template` 過**（在什麼情況下發現：要驗 `otel.logs.exporter` 時這台沒有 helm，下載執行被擋）。語法照既有 helper 的寫法，但要在有 helm 的地方跑 `sdk/ci/validate-charts.sh` 一次。
- **`logs/otlp` 這條路在 K8s 上沒跑過**：agent 真的用 OTLP 送 log、`k8s_attributes` 依連線來源補身分，這次是用 `resource` 寫死代替。要在 k3s 用 java-app 設 `otel.logs.exporter: otlp` 跑一次（`e2e/local-k3s/run-trace-e2e.sh` 可以加）。
- **開 otlp 會收到兩份 log**（stdout 一份、agent 一份）：沒有去重。要嘛 filelog 那邊對設了 otlp 的 Pod 不收（看 Pod 的 label／annotation），要嘛接受雙份、查的時候用 `agent.type` 分。先接受，試點看量。
- **純文字 pattern 有印 MDC 的格式**（`[trace_id,span_id]` 這種 Spring Cloud Sleuth 風格）pipeline 還是不認：補一條格式規則不難，但要先看有沒有這種專案。
- **metrics 的窗只驗了 Java（Prometheus 抓取）這條**：.NET 用 OTLP 推（`metrics/otlp` pipeline，`OTEL_METRIC_EXPORT_INTERVAL` 30 秒）走的是另一條，`cumulativetodelta` 在 pipeline 的最後；[dotnet.md](../metrics/dotnet.md) 在 k3s 驗過，這次沒重做。
- **假 Java 的直方圖是固定的桶**，p95 3.75 是格內插值；真的 agent 吐的是指數直方圖（ES 存成 `exponential_histogram`），percentile 的誤差不同，要用真實資料看。
- 簡報裡「持續觀察中」的四列，這次能重跑的只有關聯這一列；效能、容錯（Collector 重啟、ES 停機）、分流與 key 輪替還是只有 k3s 的紀錄（[trace-durability](2026-10-trace-durability.md)、[es-startup-401](2026-10-es-startup-401.md)、[ingest-simplify](2026-10-ingest-simplify.md)）。

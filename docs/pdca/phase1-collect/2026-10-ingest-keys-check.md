# 收值、共同鑰匙圈、log↔trace 綁定、metrics 時間窗：這週的驗證（2026-10）

**狀態**：完成（docker；補了 OTLP log 的路和 pipeline 兩個漏洞；真的 Java agent、.NET 自動埋點各跑一次）　　**日期**：2026-10-27　　**相關**：分支 `claude/ingest-check`；對照簡報「PDCA (Phase 1) 收值端整合、共同鑰匙圈與極限壓測」；[field-unify](2026-10-field-unify.md)、[trace-durability](2026-10-trace-durability.md)、[docs/rca/README「共同鑰匙圈」](../../rca/README.md#共同鑰匙圈完整清單)
**模組**：① 收值、② 清資料、③ 存

## Plan：這一次要達到什麼

- **需求**：簡報把 Phase 1 寫成「已完成（k3s 單節點驗證通過）」，Check 四列都是「持續觀察中」。要用**這週能重跑的**驗證回答三個問題：
  1. **共同鑰匙圈**：三種資料進 ES 後，用同一組鑰匙（服務、Pod、namespace、cluster、時間、`trace_id`）查得到、對得起來嗎？
  2. **自動注入（Java agent、.NET 自動埋點）的 log 跟 trace 能綁嗎？** 條件是什麼、什麼情況綁不起來？
  3. **metrics 的聚合是用時間窗嗎？** Collector、ES、baseline 每一層的「窗」對不對得上？
- **目的**：
  - 三個問題各有一個**在 docker 就能跑**的驗證（這台沒有 k3s），跟 k3s 的紀錄互相對照。
  - 問題 2 的答案寫成一張表；綁不起來的情況給做法。
  - 問題 3 缺哪一層就補程式；不缺就把三層的窗寫清楚。
- **範圍**：收值到存（①②③）＋ baseline 讀 ES 的那一段窗。不做壓測（簡報的 5,000 logs/sec 要 k3s）、不做 Kafka 串接。

### 改之前的現況

| 問題 | 現況 | 證據 |
|---|---|---|
| 共同鑰匙圈 | 三種資料同一條路徑（見下）；log 是 alias 指到 ECS 欄位 | `component-mappings.json`；k3s trace e2e 23 項 |
| log↔trace（Java agent） | agent 把 `trace_id`、`span_id` 放進 MDC；log 格式有印 MDC 才會在 stdout 上 | `sdk/standards/java/logback-spring.xml` |
| 同上 | SDK 的 `logback-spring.xml`（LogstashEncoder）會印；pipeline 認 `trace_id`／`traceId`／`trace.id` | `pipeline-fields.painless` |
| log↔trace（.NET 自動埋點） | chart 開 JSON console＋`IncludeScopes`，`TraceId` 在 `Scopes`，pipeline 會拿 | `_deployment.tpl`、[dotnet.md](../../metrics/dotnet.md) |
| 同上 | Serilog compact 的 `@tr` 也認 | `pipeline-fields.painless` |
| 綁不起來的情況 | 應用用自己的純文字 pattern（沒印 MDC），stdout 沒有 trace_id | — |
| 同上 | chart 設 `OTEL_LOGS_EXPORTER=none`；node Collector 只收 filelog，**沒有 OTLP log 的 pipeline** | `_deployment.tpl` 第 102 行、`node.yaml` pipelines |
| metrics 時間窗 | 三層（見下） | `node.yaml`、`series.yaml`、`detector/es.py` |

- 共同鑰匙圈的證據：`telemetry/storage/component-mappings.json`、[field-unify](2026-10-field-unify.md)。
- 鑰匙路徑：`resource.attributes.service.name`、`resource.attributes.k8s.{namespace,pod,cluster}.name`、`trace_id`、`span_id`；log 的 alias 指到 `service.name`、`kubernetes.*`、`trace.id`。
- metrics 的三層窗：
  1. Collector 每 30 秒抓一次，`cumulativetodelta` 把累加值換成「這 30 秒的量」。
  2. ES 存 30 秒一筆（TSDS）。
  3. baseline 用 `date_histogram` 依序列的粒度（30 秒～1 小時）加總，p95 用直方圖的 percentile。

## Do：改動哪些 code、改了什麼、為何要改

| 檔案 | 改了什麼 | 為何 |
|---|---|---|
| `e2e/pipeline/trace-cases.json`、`run-trace-cases.sh`（新） | 7 種 log 輸出丟 `k8s-logs` 的 `_simulate`，比對 `trace.id`、`span.id`、`log.level` | 問題 2 的答案要能一直重跑 |
| 同上 | 第二、三段用 `k8s-logs-otel` 模擬 agent 直送的 log record、span 的例外事件 | 同上 |
| `telemetry/collectors/node.yaml` | 新 pipeline `logs/otlp`（順序見下） | 應用不印 MDC 時，agent 直送的 log record 本身帶 trace_id；之前沒有這條路 |
| 同上 | `transform/logs-otlp-pre` 跟 trace 一樣拿掉自帶的 K8s、部門欄位 | 擋冒充 |
| `sdk/charts/app-common`（`_deployment.tpl`、`_helpers.tpl`）、`sdk/examples/mes-api/values.yaml` | `otel.logs.exporter`：`none`（預設，stdout）或 `otlp` | 應用團隊一個開關就能切 |
| 同上 | `otlp` 時多設 `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`、協定；值不對 `helm template` 會失敗 | 同上 |
| `telemetry/storage/pipeline-otel.painless` | agent 直送的 log record（有 `trace_id` 或 `severity_text`）：相關欄位帶成 `_otel` | 原本只重組 body 和 K8s 身分，trace_id 被丟掉，對不起來 |
| 同上 | 帶的是 `trace_id`、`span_id`、`severity_text`、自訂 attributes | 同上 |
| 同上 | 再重組成 Fluent Bit 的形狀 | 後面的 pipeline 照常處理 |
| `telemetry/storage/pipeline-fields.painless` | 原文裡沒有的從 `_otel` 補：`trace.id`、`span.id`、`log.level`，自訂欄位進 `app` | 同上 |
| 同上 | span 的例外事件變成的 log record（沒有 body）補 `message`、`level: error`、`error.type` | ES 的 OTLP trace 入口會把例外事件另存成一筆 log |
| `docs/trace/README.md`、`docs/rca/README.md`、`sdk/standards/tracing.md` | 問題 2 的表（哪種輸出、trace_id 在哪、綁不綁得起來、要做什麼） | |

- `logs/otlp` 的順序：otlp receiver → `transform/logs-otlp-pre` → `k8s_attributes` → `transform/group` → ES。
- 7 種 log 輸出：logback JSON、camelCase JSON、ECS JSON、.NET console＋Scopes、Serilog compact、純文字沒印 MDC、純文字有印 MDC。
- 之前文件寫例外事件「不存」，指的是 span 文件裡沒有；ES 另存成一筆 log record。

## Check：測試的目的與結果

全部在 docker：
- ES 9.5.5（`telemetry/scripts/elasticsearch-setup.sh` 裝的 pipeline、template、寫入 key）。
- otelcol-contrib 0.162.0，照 node.yaml 的處理器；`k8s_attributes` 那段用 `resource` 處理器寫死。
- 假 Java：busybox 吐 Prometheus 直方圖，每 5 秒 20 個成功＋3 個 500，500 的在 2.5～5 秒那格。
- 沒有 K8s：filelog 讀容器 log 檔、`k8s_attributes` 依 Pod 補身分、trace-sampler 尾端取樣這三段沒驗（k3s 的 e2e 有）。

### 問題 1：共同鑰匙圈

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 同一組路徑查得到、值一樣 | Collector 送一個 trace（兩段，帶例外事件）＋同 trace_id 的 log＋metrics；用 `fields` 取鑰匙 | trace、log 都在 `*-it`；值全部一樣（mes-api／mes-prod／mes-api-7d9f-abcde／c1／D3120／it） |
| 防冒充 | 同上，應用自帶假 namespace、`dept: D9999` | 被拿掉 |
| trace → log | 拿 SELECT 那段的 `span_id` 查 log | 查到那行「Connection pool timeout」 |
| log → trace | 拿 log 的 `trace_id` 查 trace | 兩段都在，例外的 type 抄到 span 上 |
| metrics → trace | 拿 metrics 的服務＋時間查 trace | 同一個 trace |

- trace 內容：mes-api `GET /lots/{id}` → `SELECT mes.lots` 出錯。
- 取的鑰匙：`resource.attributes.service.name`、`k8s.{namespace,pod,cluster}.name`、`dept`、`group`、`trace_id`、`span_id`。

### 問題 2：log↔trace

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| 哪些輸出格式綁得起來 | `run-trace-cases.sh` 7 案例 | 5 種 JSON 都拿到 `trace.id`、`span.id`、level；純文字兩種拿不到 |
| OTLP 直送，修 pipeline 前 | 第二段（`_simulate`）＋驗證 B 真的送一筆 | `k8s-logs-otel` 把 trace_id 丟了（hits 0） |
| OTLP 直送，修 pipeline 後 | 同上 | `trace_id`、`span_id`、`log.level: error`、`app.lot_id`、`app.thread.name` 都在 |
| 同上 | 同上 | service.name 從 pod label 補上 |
| span 的例外事件 | 依 trace_id 查 log | 回 2 筆，第二筆是例外事件（`event.name: exception`，沒有 body） |
| 同上 | 第三段案例 | 補 message、level、error.type 後是一筆有用的 log |
| **D：真的 Java agent** | Tomcat 純文字 log ＋ agent ＋ `OTEL_LOGS_EXPORTER=otlp`（見下） | ES 的 `k8s-it-c1` 裡同一行帶 `trace_id: ee5ab1…`、`span_id: bedd5a…`、`log.level` |
| 同上 | 拿這個 trace_id 查 traces | `GET /index.jsp` 的 Server span，span_id 一樣 |
| **D：真的 .NET 自動埋點** | ASP.NET Core 預設純文字 console ＋ 自動埋點 ＋ otlp（見下） | 同一個請求 6 筆 log 都帶 `trace_id: aa67f9…` |
| 同上 | 同上 | 6 筆＝應用 2 筆＋框架的 Executing／Executed／Request finished |
| 同上 | `LogError` 那筆 | `log.level: error`；span_id 對到 `GET /orders/{id:int}` 的 Server span |

- 純文字兩種拿不到 trace_id 是預期的，這就是要補 OTLP 那條路的原因。
- Java 的 D：`tomcat:10.1-jdk21` ＋ `JAVA_TOOL_OPTIONS=-javaagent:…`（chart 用的 autoinstrumentation-java 2.10.0）；JSP 用 `java.util.logging` 印「order N handled」、錯誤。
- Java 的 stdout：Tomcat 純文字（`27-Oct-2026 07:13:58 INFO [http-nio-8080-exec-2] … order 2 handled`），沒有任何 trace 欄位。
- .NET 的 D：`mcr.microsoft.com/dotnet/aspnet:8.0` ＋ chart 同一組 `CORECLR_*`、`DOTNET_STARTUP_HOOKS`（autoinstrumentation-dotnet 1.12.0）＋ `OTEL_DOTNET_AUTO_LOGS_INCLUDE_FORMATTED_MESSAGE=true`。
- .NET 的程式只有 `ILogger.LogInformation`／`LogError`，**沒有** chart 的 `Logging__Console__*` JSON、scope 設定；stdout 是 `info: Program[0]` ＋ 下一行 `order 500 handled`。
- 兩個 D 的程式裡都沒有任何 OpenTelemetry 的東西，log 格式也沒印 trace 欄位。

### 問題 3：metrics 時間窗

| 目的 | 怎麼測 | 結果 |
|---|---|---|
| ES 裡每筆的窗 | 假 Java；Collector 每 10 秒抓、`cumulativetodelta` | 每分鐘 12 筆（200、500 各 6 筆）；每筆 `start_timestamp` 到 `@timestamp` 剛好 10 秒 |
| 同上 | 同上 | count 6 ＝ 3 × 2 個 5 秒 |
| baseline 的 1 分鐘窗 | baseline 的 `fetch` 用 1 分鐘的窗加總 | `http.requests` **276／分**（＝ 23 × 12） |
| 同上 | 同上 | `http.error_rate` **13.04%**（＝ 36/276） |
| 同上 | 同上 | `http.latency_p95_s` **3.75 秒**（500 的都在 2.5～5 秒那格，格內插值） |
| 穩不穩 | 連續兩分鐘 | 一模一樣 |
| Collector 設定還是合法的 | `otelcol validate` common.yaml＋node.yaml（掛假的 serviceaccount token、自簽 CA） | 改前改後都過 |

### 對照簡報的 Check

- 「關聯機制驗證：可透過共同鑰匙圈及 trace_id 正確串接」：docker 重做是成立的，但**只對 log 格式有印 trace_id 的應用成立**。
- 純文字 pattern 的舊專案要開 `otel.logs.exporter: otlp`；這條路在這次之前不通（pipeline 會丟 trace_id）。
- 「收值效能 5,000 logs/sec、0 遺失」這次沒測（要 k3s）。

## Act：Check 後沒改善的、還沒解決的

### .NET Framework（4.x）吃不吃得進去

在什麼情況下發現：問「大部分是 C#」時。依據：OpenTelemetry .NET 自動埋點官方文件（opentelemetry.io/docs/zero-code/dotnet、config.md，2026-10-27）。

| signal | .NET Framework 4.x | 文件怎麼寫 |
|---|---|---|
| trace、metrics | **可以**：Windows x64，IIS、Windows Service 都掛得上（CLR profiler） | 「The minimal supported version of .NET Framework is 4.6.2」 |
| log 直送（ILogger） | **不行**；而且要 `Microsoft.Extensions.Logging` ≥ 8 | 「Not supported on .NET Framework」 |
| log4net ≥ 2.0.13、NLog ≥ 5.0 | 可能可以，**沒驗過** | bytecode instrumentation，「trace context injection and a logs bridge」；標 Experimental |

- .NET Framework 的舊專案多半也不是用 ILogger。
- trace、metrics 照 K8s 上 .NET 的做法，但要在 Windows VM 上跑（Framework 進不了 Linux 容器）。
- log：log4net／NLog 5 以上可能走得通（要驗）；自己寫檔的只能用服務＋時間對。

### 其他

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| chart 沒 `helm template`；`logs/otlp`、`k8s_attributes` 在 K8s 上沒跑 | 這台沒有 helm、沒有 K8s | 身分是 Collector 設定寫死的 | 2026-10-27 已在 k3d 驗（見下）；.NET 在 K8s 上沒跑 |
| .NET 開 otlp 後框架的 log 也進來 | D：一個請求 6 筆，4 筆是 ASP.NET Core | 量比 stdout 大很多 | 正式環境保持 `Logging__LogLevel__Microsoft.AspNetCore` = Warning |
| 開 otlp 會收到兩份 log（stdout、agent） | 設計時 | 沒有去重 | 先接受，試點看量 |
| 純文字有印 MDC 的格式（`[trace_id,span_id]`）pipeline 不認 | 寫案例時 | Spring Cloud Sleuth 風格的專案綁不起來 | 補規則不難；先看有沒有這種專案 |
| metrics 的窗只驗了 Java（Prometheus 抓取） | 問題 3 | .NET 用 OTLP 推，走另一條 | 這次沒重做（見下） |
| 假 Java 的直方圖是固定桶，p95 3.75 是格內插值 | 問題 3 | 真的 agent 吐指數直方圖，percentile 誤差不同 | 用真實資料看 |
| 簡報「持續觀察中」四列只重跑了關聯 | 對照簡報 | 效能、容錯、分流與 key 輪替只有 k3s 的紀錄 | 見下 |

- k3d 的結果：`helm template` 過，`logs/otlp` 在 K8s 上 Java 通了（[k3d-e2e](2026-10-k3d-e2e.md)）；.NET 要用 `e2e/local-k3s` 的 dotnet-app 設 `otel.logs.exporter: otlp` 跑。
- chart 對 .NET 已把 `Logging__LogLevel__Microsoft.AspNetCore` 設成 Warning，走 otlp 的 log 一樣受管；D 的測試沒設，所以都進來了。
- 兩份 log 的做法：filelog 對設了 otlp 的 Pod 不收（看 Pod 的 label／annotation），或接受雙份、查的時候用 `agent.type` 分。
- .NET 的 metrics 走 `metrics/otlp` pipeline（`OTEL_METRIC_EXPORT_INTERVAL` 30 秒），`cumulativetodelta` 在 pipeline 最後；[dotnet.md](../../metrics/dotnet.md) 在 k3s 驗過。
- 效能、容錯（Collector 重啟、ES 停機）、分流與 key 輪替的紀錄：[trace-durability](2026-10-trace-durability.md)、[es-startup-401](2026-10-es-startup-401.md)、[ingest-simplify](2026-10-ingest-simplify.md)。

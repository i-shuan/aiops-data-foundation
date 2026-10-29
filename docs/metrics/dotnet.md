# 應用是 .NET 時：為什麼這樣做

> **類型：原理 Explanation**　同主題：[原理](README.md)・[規格（欄位）](reference.md)・[操作（部署、驗證）](how-to.md)・[指標目錄（規格）](catalog.md)・[metrics 能做什麼（原理）](capabilities.md)・[案例與實測](cases.md)・[.NET 操作](dotnet-how-to.md)・[.NET 規格](dotnet-reference.md)

一句話：**不改程式。** chart 開 `otel.dotnet`，掛 OpenTelemetry .NET 自動埋點（跟 Java agent 一樣的做法）；metrics、trace 都由應用推給同節點的 Collector（OTLP），log 的格式也用環境變數設定。平台端（ES、baseline、trace baseline、根因分析）不用換，規則多了 .NET 自己的幾條（Gen2 GC、例外、執行緒池）。

實測：真的 ASP.NET Core 8（當作幾年前留下來、不能改的舊專案）＋ 自動埋點 v1.12.0 ＋ PostgreSQL（Npgsql），腳本 [e2e/local-k3s/run-dotnet-e2e.sh](../../e2e/local-k3s/run-dotnet-e2e.sh)，結果見後面。之前也用 .NET 9 跑過一輪，全部通過。

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
| HTTP 指標 | `http_server_request_duration_seconds`（node Collector 改成 `http.server.request.duration`） | `http.server.request.duration`（**同一個指標**，OTLP 保留原名）；**預設沒開，chart 會加** |

## 選項 B：程式裡加 SDK＋拉（能改程式的新專案可以考慮）

能改程式的新專案也可以在程式裡加 OpenTelemetry SDK，開 `/metrics` 讓 Collector 抓，好處是指標名稱跟 Java 一樣（`http_server_request_duration_seconds`、`db_client_connections_*`）、部門照 namespace。目前平台**沒有**為它準備 chart 模式和規則（採用的是 A）；要用的話先討論。

2026-10-10 實測過一輪（ASP.NET Core 8 ＋ SDK 1.19，程式在 git 歷史 `4a36110` 的 `e2e/local-k3s/dotnet-app/`）：資料、log 對 trace、正常不報、連線池用光、執行緒暴增（44 條）、洩漏（Gen2 第 7 分鐘、到底一直丟例外）全部通過。踩到的：

- 要加 8 個套件，其中 `OpenTelemetry.Exporter.Prometheus.AspNetCore` 一直是 beta、`Instrumentation.Process` 是 rc。
- Collector 每次來抓也算一個請求（`http_route="/metrics"`），baseline 要排除；`/metrics` 只開在 9464（`RequireHost("*:9464")`）。
- **`AddOtlpExporter()` 不認 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`／`_PROTOCOL`**（Java、自動埋點用的那組）：trace 照預設送 `localhost:4317`，連不上一直重試、**每次重試都丟例外**（正常時 5 分鐘 574 次），讓例外規則誤報。要給通用的 `OTEL_EXPORTER_OTLP_ENDPOINT`、`OTEL_EXPORTER_OTLP_PROTOCOL`。找原因的方法：程式目錄放 `OTEL_DIAGNOSTICS.json`（自我診斷 log）。
- `OpenTelemetry.Exporter.OpenTelemetryProtocol` 1.9.0 有已知弱點警告，要用 1.19.x。

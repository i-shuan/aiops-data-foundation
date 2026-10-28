# 不改程式的自動埋點：trace、traceparent、log 的 trace_id（2026-10）

**狀態**：已完成　　**日期**：2026-10-28　　**相關**：分支 `claude/p1-sdk-7146f3`（從 `claude/phase1-collect-verify` 切出）
**模組**：① 收值

## Plan：這一次要達到什麼

- **需求**：應用團隊不改程式、只掛 Java agent 或 .NET 自動埋點，log 跟 trace 能不能在 ES 對起來，之前只驗過「一個請求」的基本情況；子執行緒、背景排程、上游帶怪的 traceparent、多跳、Kafka 時 log 帶哪個 trace_id 沒人測過。
- **目的**：每個情境寫下預期，在 K8s 上實際跑，ES 裡的 log `trace.id`／`span.id` 跟 span 的 `trace_id`、`parent_span_id`、`kind` 對照；不符合的寫成給應用團隊的注意事項。
- **範圍**：
  - Java：agent 2.20.0，JDK 21，JDK 內建 HTTP server、`java.net.http.HttpClient`、kafka-clients 3.7.1，log 用 SDK 的 [logback-spring.xml](../../../sdk/standards/java/logback-spring.xml)。
  - .NET：**只測 .NET 8**（ASP.NET Core 8），自動埋點 1.12.0，`HttpClient`、Confluent.Kafka 2.6.1，log 用內建 JSON console＋`IncludeScopes`（跟 chart 一樣用環境變數開）。
  - 不測：Spring（`@Async`、`@Scheduled`）、.NET 9、.NET Framework、Kafka 批次消費與重送、取樣（測試環境的 trace-sampler 全部留）。

## Do：改動哪些 code、改了什麼、為何要改

無平台 code 改動。新增可重跑的驗證（`e2e/local-k3s/sdk-inject/`）：

| 檔案 | 內容 | 為何 |
|---|---|---|
| `java/SdkApp.java` | 單檔 Java 應用，程式裡沒有 OpenTelemetry | 每個情境一個端點，log 都寫 rid |
| `dotnet/Program.cs`、`app.csproj` | ASP.NET Core 8 應用，同上 | 同上；.NET 多 `async`、`Task.Run` |
| `java.yaml`、`dotnet.yaml` | Deployment 範本，各部署 a（消費 Kafka）、b | 兩跳、跨語言要兩台以上 |
| `base.yaml` | Kafka 3.9.1（KRaft）、發請求用的 curl Pod | — |
| `run-sdk-inject-e2e.sh` | 部署、探測 trace 路徑、發請求、逐項印 PASS／FAIL | 重跑：`SKIP_DEPLOY=1` |

- 腳本從部署到檢查完整段拿著 cluster 鎖；在鎖裡才 port-forward ES，斷了（ES Pod 被重建）自動重連，每次查 ES 連不上會等最多 2 分鐘。
- 量之前先確認最近 1 分鐘有 span 進 ES、再發 6 個探測請求（只通一台 trace-sampler 會缺一半）；不通就停下來說原因，不動 cluster，不會印出一堆假 FAIL。

文件：[sdk/standards/tracing.md](../../../sdk/standards/tracing.md)（log 什麼時候帶 trace_id、2-4）、[troubleshooting.md](../../troubleshooting.md)、[metrics/dotnet.md](../../metrics/dotnet.md) 的 Kafka、[trace/README.md](../../trace/README.md)。

## Check：測試的目的與結果

rancher-desktop（k3s 單節點），21 項全部 PASS 的兩輪結果一樣（第 11 次是整段拿著 cluster 鎖、重新部署再跑）：

| 輪 | 時間（UTC） | 當時的 cluster | log 進 ES 延遲 |
|---|---|---|---|
| 第 8 次 | 2026-10-27 19:24 | master 版 Collector | 約 1 秒 |
| 第 11 次 | 2026-10-28 00:53～00:56 | `6c59f67` 版 Collector；`k8s-imc-local` 00:50:54 rollover 過 | 約 0 秒；最後一筆 10 秒內到齊 |

「實際」欄的 ID 取前 4 碼，是第 8 次的值（rid 後綴 `1793129039`，在 `k8s-*` 查得到；第 11 次是 `1793148916`）。

**Java（agent 2.20.0）**

| 情境 | 預期 | 實際 |
|---|---|---|
| 基本（沒有上游） | log 有 trace_id、span_id；對到最上層 Server span | log `1856…`／`dfdc…`，Server span 沒有 parent |
| 上游 traceparent 正常 | 沿用上游 trace_id；Server 的 parent 是上游 span | log `8647…`＝上游；Server parent `09d8…`＝上游 |
| 上游 traceparent 格式錯（`00-xyz-123-01`） | 當作沒有，開新 trace | 新 trace `9632…`，Server 最上層 |
| 上游未取樣（`-00`） | log 仍帶上游 trace_id；往下傳 `-00`；ES 沒有 span | a、b 的 log 都是 `442e…`；b 收到 `-00`；ES 0 段 |
| 兩跳 java-a→java-b→dotnet-a | trace_id 一致；每跳的 parent 是上一跳 Client | 三跳 log 都 `baa9…`；收到的 parent 各是上一跳 Client |
| `ExecutorService` | 子工作 log 同 trace、同 span；呼叫在請求底下 | parent、child 都 `543c…`／`5101…`；呼叫同 trace |
| `CompletableFuture.supplyAsync` | 同上 | parent、child 都 `a7a8…`／`fe69…`；呼叫同 trace |
| 自己 `new Thread` | 子 log 沒 trace_id；呼叫自成新 trace | child log 沒 trace_id；呼叫是新 trace `338c…`，最上層 Client |
| 背景排程（沒有上游請求） | log 沒 trace_id；呼叫自成 trace | log 沒 trace_id；呼叫的 trace 最上層是 Client |
| Kafka 消費端 | 消費端 log 帶生產者的 trace_id | consumed log `99a4…`＝生產；process span 接在 publish 下 |

**.NET 8（自動埋點 1.12.0）**

| 情境 | 預期 | 實際 |
|---|---|---|
| 基本（沒有上游） | log 有 trace_id、span_id；對到最上層 Server span | log `2186…`／`cb46…`，Server span 沒有 parent |
| 上游 traceparent 正常 | 沿用上游 trace_id；Server 的 parent 是上游 span | log `2580…`＝上游；Server parent `d2f5…`＝上游 |
| 上游 traceparent 格式錯 | 當作沒有，開新 trace | 新 trace `fc2d…`，Server 最上層 |
| 上游未取樣（`-00`） | log 仍帶上游 trace_id；往下傳 `-00`；ES 沒有 span | a、b 的 log 都是 `2b56…`；b 收到 `-00`；ES 0 段 |
| 兩跳 dotnet-a→dotnet-b→java-a | trace_id 一致；每跳的 parent 是上一跳 Client | 三跳 log 都 `b7f8…`；收到的 parent 各是上一跳 Client |
| `await` 之後 | 子工作 log 同 trace、同 span；呼叫在請求底下 | parent、child 都 `5a23…`／`7280…`；呼叫同 trace |
| `Task.Run` | 同上 | 都 `fced…`／`8069…`；呼叫同 trace |
| `ThreadPool.QueueUserWorkItem` | 同上 | 都 `ddc9…`／`7e97…`；呼叫同 trace |
| 自己 `new Thread` | 同上（ExecutionContext 會帶到新執行緒） | 都 `8cfb…`／`0576…`；呼叫同 trace |
| 背景 `Task.Run`（啟動時開） | log 沒 trace_id；呼叫自成 trace | log 沒 trace_id；呼叫的 trace 最上層是 Client |
| Kafka 消費端 | 消費端 log 沒 trace_id；處理時的呼叫自成 trace | consumed log 沒 trace_id；呼叫是新 trace `d62c…`（Client 最上層） |

Kafka 兩種語言的 span 長得不一樣（同一輪，從 `traces-*` 查）：

| 語言 | 生產者 trace 裡有 | 消費端的 span |
|---|---|---|
| Java | Server → `sdk-java publish` → `process` → Client → Server | `process`（Consumer），parent 是 publish |
| .NET | Server → `sdk-dotnet publish` | `receive` 自成 trace `1df8…`，link 指回 publish |

- 測的過程發現：master 版 `node.yaml` 部署後 node Collector 對所有 trace 回 `couldn't find the exporter for the endpoint`，探測請求的 log 進了、span 沒進（見 Act）。
- 第 9 次 log 進 ES 慢到約 58 秒，19 項 FAIL（log 還沒進就查）：當時 p1-load 在同一個 cluster 壓測、Collector 積壓，不是 SDK 的問題。腳本改成輪詢到最後一筆 log、span 進 ES（上限 5 分鐘）並印出延遲。
- Java 的 `trace_id` 是 agent 在 log 事件上加的：LogstashEncoder 的 JSON 有，程式裡 `MDC.get("trace_id")` 拿不到。
- **沒測到的**：Spring 的 `@Async`、`@Scheduled`（[tracing.md](../../../sdk/standards/tracing.md) 第 1 節列為自動）；chart 用的 agent 2.10.0（這次是 2.20.0）；.NET 9、Framework；Kafka 一次 poll 多筆、重送；尾端取樣 5% 時的行為。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| [①] master 版 node Collector 重啟後 trace 全被拒 | 跑驗證時 ES 沒有任何新 span，log 有 | 不掉 log，trace 全掉，不會自己好 | p1-durability 的分支在修；腳本偵測到就停、回報平台 |
| Java 自己 `new Thread` 的 log 沒 trace_id | 本輪 Java `new Thread` 情境 | 那段 log 對不到請求 | 已寫在 tracing.md 2-4：用 `Context.current().wrap` |
| .NET Kafka 消費端 log 沒 trace_id | 本輪 .NET Kafka 情境 | 消費端的錯誤 log 找不到是哪個請求送來的 | 不改程式時靠業務 ID（[trace/README.md](../../trace/README.md) 做法 C） |
| 上游送 `-00`：log 有 trace_id，ES 沒那個 trace | 本輪未取樣情境 | 從 log 點 trace_id 查不到 trace | 要不要在入口改掉外部來的 `-00`，未決定 |
| chart 的 agent 是 2.10.0，這次測 2.20.0 | 對照 tracing.md 第 1 節 | 2.10.0 的結果未驗證 | 升 chart 的 agent 版本時用這支腳本重跑 |

- **決定**：不改平台；給應用團隊的注意事項寫進 tracing.md。
- **下一輪**：Spring Boot（`@Async`、`@Scheduled`、`@KafkaListener`）用同一支腳本補測。

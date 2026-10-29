# 應用是 .NET 時：操作

> **類型：操作 How-To**　同主題：[原理](README.md)・[規格（欄位）](reference.md)・[操作（部署、驗證）](how-to.md)・[指標目錄（規格）](catalog.md)・[metrics 能做什麼（原理）](capabilities.md)・[案例與實測](cases.md)・[.NET 原理](dotnet.md)・[.NET 規格](dotnet-reference.md)

chart 怎麼開、Collector 怎麼接、Kafka 怎麼接、遇到問題怎麼辦。為什麼這樣做見 [dotnet.md](dotnet.md)。

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

node Collector 有一條 `metrics/otlp`：OTLP 收進來的 metrics → 拿掉應用自填的 K8s、部門欄位 → 依連線來源 IP 補上 Pod、部門 → 直方圖換成差值 → 依部門送進 `metrics-generic.otel-<group>`（[node.yaml](../../telemetry/collectors/node.yaml)）。

## 坑

集中在 [troubleshooting.md](../troubleshooting.md#net)。

## Kafka（Confluent.Kafka）

自動埋點支援 Confluent.Kafka 1.4～2.x，不改程式時（k3s 實測，[docs/trace/scenarios.md 場景四](../trace/scenarios.md#四非同步訊息佇列)）：

- **生產端**：HTTP 請求 → `orders publish` 在同一個 trace。
- **消費端**：`orders receive` 是**新的 trace**，只用 link 指回生產端；它的時間是 `Consume()` 等下一筆訊息等了多久，不是處理時間。
- **消費端處理時查的 DB、呼叫的 API 每一筆都自成一個 trace**，對不回是哪一筆訊息。
- **消費端處理時印的 log 沒有 trace_id**（[pdca/phase1-collect/2026-10-sdk-inject.md](../pdca/phase1-collect/2026-10-sdk-inject.md)）。Java agent 不一樣：處理的 span 接在 publish 底下，log 帶生產者的 trace_id。

平台的做法：排隊時間從 link 找生產端算；沒有上一層的 DB span 當成自己的入口學 baseline（看得到消費者寫 DB 變慢、出錯）；積壓看 kafka_exporter 的 lag。要整條串起來得在消費端改程式（約 5 行，用 header 的 `traceparent` 開處理的 span），目前不做。

# Trace 進 Elasticsearch

一句話：**應用把 trace 全部送給同一個節點上的 OpenTelemetry Collector，Collector 依「送來的連線是哪個 Pod」讀 namespace 的 dept 分 group；再依 trace_id 送到第二層 trace-sampler，等整個 trace 到齊後決定留不留（錯的、慢的、吞掉例外的全留，正常的隨機留 5%），留下的各 group 用自己的 key 寫進 ES。**

收 log、metrics 的也是同一個 Collector（[log/README.md](../log/README.md)、[metrics/README.md](../metrics/README.md)），分部門的規則和 key 共用。

## 現況與待辦（①收值、②清資料、③存）

**已完成 ✅**

| 項目 | 狀態 |
|---|---|
| Java agent、.NET 自動埋點，不改程式；應用全部送到同節點的 Collector | ✅ |
| 依連線來源的 Pod 分部門、擋冒充 | ✅ |
| 尾端取樣：兩層 Collector，錯的、慢的（> 1 秒）、有例外的全留，正常的隨機留 5%；每一段記著是哪條規則留的 | ✅ |
| 例外事件抄到 span 屬性（ES 會丟掉事件） | ✅ |
| 跟 log 串：`trace_id` ↔ log 的 `trace.id` | ✅ |
| 重送不重複（`trace_id + span_id` 當 `_id`） | ✅ |

**已完成的部分，還差一點**

| 項目 | 要做的 |
|---|---|
| ① trace-sampler 斷線 | ✅ 兩段佇列都改寫在磁碟：node 端寫節點磁碟、trace-sampler 端寫 PVC。實測 trace-sampler 停 400 秒、node 重啟、ES 停機中 trace-sampler 重啟都不掉；**trace-sampler 當掉（kill -9）仍掉約 20%**（等 30 秒決定的那段在記憶體）。不用 Kafka（實測沒有多擋住任何情況）。[PDCA](../pdca/phase1-collect/2026-10-trace-durability.md) |
| ① ES 剛起來回 401、Collector 直接丟 | ✅ 修在 ES 入口：健康檢查（LB 或 readinessProbe）要用 API key 打 GET /、等 200；ES 剛起來的 1 秒多 API key 會被拒，但 elastic 帳號、`/_cluster/health` 都正常，用它們檢查會把流量導進去。實測直連掉 420、經健康檢查的 LB 掉 0；兩台 ES 滾動重啟用舊檢查掉 200～280、用新檢查掉 0（兩台的投票名單只有一台，重啟到它就沒 master；三台以上才能真的滾動）。📝 中央 ES 前面的健康檢查要請管 ES 的人改（範例 `telemetry/storage/healthcheck-haproxy.cfg`）。[PDCA](../pdca/phase1-collect/2026-10-es-startup-401.md) |
| ③ 保留天數 | ✅ 隨機留下的 7 天；有問題的（錯誤、5xx、例外、慢）由 pipeline 轉到 `traces-problem.otel-<group>`，留 30 天（[retention.md](../retention.md)） |

**要做的（①收值、②清資料、④判斷）**

### 時鐘差（時間對齊）

*模組：④判斷（規則）。*

- ✅ 已有規則：呼叫方和被呼叫方的時間差超過 500ms 就記（`trace.clock_skew_ms`）。
- 📝 每天列出時鐘差最大的前 10 對服務。主機本身的時鐘監控見 [metrics/README.md](../metrics/README.md)。

### Kafka 串接

*模組：①收值（埋點要能接回原本的請求），為了讓 ⑦根因分析不在 Kafka 斷掉。*

**為什麼**：請求進 Kafka 後，消費者處理時查的 DB、打的呼叫要能接回原本的請求，不然根因分析在 Kafka 這裡就斷了。

**現況**（[scenarios.md 場景四](scenarios.md#四非同步訊息佇列)）：

- ✅ 排隊時間、消費落後（kafka_exporter）抓得到。
- ✅ 消費端的 `receive` span 有 link 指回生產端的 `publish`。
- ❌ **.NET 不改程式時，消費者處理訊息時的 DB、呼叫自成一個 trace**（k3s 實測 961／961 筆都是孤兒），接不回訊息。
- ❓ Java agent 理論上會自動帶 header、自動串起來，還沒在 k3s 驗證。

| 路線 | 內容 | 代價 | 建議 |
|---|---|---|---|
| A. 驗證 Java | k3s 加 Java 的生產者、消費者，確認不改程式就能串起來 | 我們，1～2 天 | 先做：確認問題範圍是不是只有 .NET |
| B. .NET 加幾行 | 消費者收到訊息時，從 header 取出 trace context 當成處理時的上層（約 5 行） | 各應用團隊改程式 | 只對重要的消費者要求；之前決定先不改，列為「之後談」 |
| C. 業務 ID 事後關聯 | 訊息帶交易 ID、單號，生產端和消費端的 log 都記這個 ID，用 ID 接起來 | 依賴下面的業務 ID | 不改程式時的退路 |

**驗收**：有 trace 能從 HTTP 入口一路看到消費者處理時查的 DB（A／B）；或給一個交易 ID 能列出生產、消費兩邊的 log（C）。

### 業務 ID（服務層）

*模組：②清資料（抽成標準欄位）。*

**為什麼**：trace 斷掉的地方（Kafka 不改程式、批次工作、排程、呼叫外部系統）只能靠業務上的 ID 接起來；根因報告也要能說「影響了哪些交易、哪些單」。

**現況**：

- ✅ 工廠用的欄位已定案：`lot.id`、`equipment.id`、`work_order.id`、`recipe.id`（[sdk/standards/tracing.md](../../sdk/standards/tracing.md)）。工廠的其餘欄位不在目前範圍。
- ❌ 服務層通用的「交易 ID」（一次操作從頭到尾的 ID，跨同步、非同步）還沒定名稱。
- ❌ 很多 ID 只寫在 log 訊息裡，沒有獨立欄位。

| 步驟 | 內容 | 誰 | 驗收 |
|---|---|---|---|
| 1. 訂名稱 | 例如 `txn.id`，跟已定案的 `work_order.id` 等並列；新增要先跟平台確認 | 我們 | 寫進 [tracing.md](../../sdk/standards/tracing.md) |
| 2. 不改程式：從 log 抽出來 | 中央 pipeline 依格式抽成標準欄位（[log/README.md](../log/README.md)） | 我們（要幾行 log 樣本） | 舊服務的 log 查得到 |
| 3. 新程式：放 MDC／scope、span 屬性 | 入口把 ID 放進 log 的 MDC（Java）或 scope（.NET），重要的也放成 span 屬性 | 各應用團隊（寫進開發規範） | 新服務的 log、trace 都帶 |
| 4. 查詢工具 | 給一個交易 ID，列出所有服務的 log、trace，依時間排序 | 我們 | 輸入一個 ID 看得到完整時間線 |
| 5. 根因報告帶 ID | 報告列出受影響的交易、單號 | 我們 | 報告裡有「影響哪些交易」 |

**一定要注意**：業務 ID **只放 log、trace，不放 metrics**（每個值一條新序列，序列數會爆炸）。

## 資料怎麼走

```
應用（OpenTelemetry Java agent 等）
   ↓ OTLP 送到 Service otel-node-collector（internalTrafficPolicy: Local → 只連同一個節點的 Collector）
節點的 Collector
   ↓ 1. 擋冒充：應用自帶的 k8s.*、container.*、dept、group、data_stream.* 一律拿掉
   ↓ 2. 看連線的來源 IP 是哪個 Pod → 補上 namespace、Pod、Deployment、namespace 的 dept、服務版本
   ↓ 3. dept → group（跟 log、metrics 同一份規則）
   ↓ 4. 例外事件的種類、訊息抄到 span 的屬性（exception.type、exception.message）：ES 不存 span 的事件
   ↓ 5. 依 trace_id 送到第二層（loadbalancing exporter）：同一個 trace 的每一段一定到同一台
trace-sampler × 2（StatefulSet，尾端取樣）
   ↓ 等 30 秒（整個 trace 到齊）再決定：隨機 5%、錯誤、5xx、例外、超過 1 秒 → 留；其他丟
   ↓ 每一段記下是哪條規則留的（scope.attributes.tailsampling.policy）
   ↓ 一個出口、一把寫入 key（ES_WRITER_API_KEY）；data_stream.namespace = group → traces-generic.otel-<group>
        ↓ 中央 ES：traces-otel pipeline 用 trace_id-span_id 當 _id（重送不會重複）
   AIOps 用唯讀 key 讀 traces-*.otel-*；人用各 group 的唯讀角色
```

**為什麼要「只連同一個節點」：** Collector 只記得自己節點上的 Pod（省記憶體），要靠連線的來源 IP 對到 Pod，所以應用一定要連到自己節點上那個。Service 的 `internalTrafficPolicy: Local` 保證這件事，也省掉跨節點的流量。

**為什麼要先拿掉應用自帶的欄位：** trace 是應用自己送的，資源欄位應用可以亂填（例如自稱是別的 namespace、別的部門）。拿掉之後，歸屬只看連線來源，應用無法冒充。

**為什麼要兩層、等多久、留哪些：** 見 [trace/scenarios.md「取樣」](scenarios.md#橫跨所有場景的前提取樣)。k3s 實測每秒 500 個請求：有問題的 100% 留下而且完整、總量剩約 15%、trace-sampler 每台約 150MB（[e2e/local-k3s/tail-sampling](../../e2e/local-k3s/tail-sampling/README.md)）。設定在 [sampler.yaml](../../telemetry/collectors/sampler.yaml)（規則）、[trace-sampler.yaml](../../telemetry/collectors/trace-sampler.yaml)（台數、等多久、隨機幾 %）。

## 應用要做什麼

用 app-common chart 而且開了 Java agent（`otel.javaAgent.enabled: true`）或 .NET 自動埋點（`otel.dotnet.enabled: true`）的服務不用做任何事，chart 會設好：

| 環境變數 | 值 |
|---|---|
| `OTEL_TRACES_EXPORTER` | `otlp` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | `http://otel-node-collector.otel-collector.svc:4318/v1/traces` |
| `OTEL_TRACES_SAMPLER` | `parentbased_traceidratio`：上游決定要記的跟著記，自己開始的請求依比例 |
| `OTEL_TRACES_SAMPLER_ARG` | `otel.traces.sampleRatio`，預設 `1.0`（全部送）。**保持 1.0**：要不要留由 trace-sampler 在請求結束後決定（尾端取樣）；在應用裡調低的話，出事的請求在一開始就被丟了 |

沒用 chart 的服務，自己設上面這幾個環境變數。不是 Java、.NET 的服務，用該語言的 OpenTelemetry SDK，送到同一個位址。

**什麼時候要自己埋點、怎麼埋（業務步驟、批號這類欄位、SECS 等 agent 不認得的元件），見 [sdk/standards/tracing.md](../../sdk/standards/tracing.md)。**

## 跟 log、metrics 串起來

- **log ↔ trace：** 自動埋點（Java agent、.NET 自動埋點）會把 `trace_id`、`span_id` 放進每一行 log 的 context（Java 的 MDC、.NET 的 scope）；中央 pipeline 存成 log 的 `trace.id`、`span.id`，alias `trace_id`、`span_id` 跟 trace 同名。實測兩邊對得到（見「驗證」）。AIOps 看到一筆錯誤 log，可以直接查那個請求的完整 trace。**綁得起來的條件是 log 的輸出格式有把 context 印出來**，不同情況如下（[pdca/phase1-collect/2026-10-ingest-keys-check.md](../pdca/phase1-collect/2026-10-ingest-keys-check.md) 逐一驗過）：

  | 應用的 log 怎麼輸出 | trace_id 在哪 | 綁得起來？ | 要做什麼 |
  |---|---|---|---|
  | Java，照 SDK 的 [logback-spring.xml](../../sdk/standards/java/logback-spring.xml)（LogstashEncoder） | JSON 的 `trace_id`、`span_id`（MDC 自動帶出） | ✅ | 不用 |
  | Java，自己的 JSON encoder | `traceId`／`trace.id` 等常見寫法 | ✅ | 不用，pipeline 認得幾種寫法 |
  | .NET，內建 JSON console（chart 預設用環境變數打開 `IncludeScopes`） | `Scopes` 裡的 `TraceId` | ✅ | 不用 |
  | .NET，Serilog compact JSON | `@tr`、`@sp` | ✅ | 不用 |
  | **純文字 pattern，沒印 MDC**（舊專案常見） | stdout 上根本沒有 | ❌ | 二選一：(a) 改 log 設定印 MDC（Java 加 `%X{trace_id}`，但 pipeline 要有那種格式的解析規則）；(b) **chart 設 `otel.logs.exporter: otlp`**：agent 把 log record（本身帶 trace_id）直接送給 Collector，不經 stdout，什麼格式都綁得起來 |
  | 純文字 pattern 有印 MDC，但 pipeline 沒有那種格式的解析規則 | 在原文裡，拆不出來 | ❌ | 同上 (b)，或補一條格式規則 |

  (b) 的代價：stdout 照印，平台會收到兩份（檔案那份沒 trace_id），所以只在需要時開；node Collector 的 `logs/otlp` pipeline 收這條。**(b) 真的 agent 驗過**（docker，2026-10-27）：Tomcat 10 的 JSP 用 `java.util.logging` 印純文字、ASP.NET Core 8 用 `ILogger` 預設純文字 console，都不改程式，ES 裡的 log 帶 trace_id、對得到 Server span。

  **.NET Framework（4.x）**：trace、metrics 可以（自動埋點支援 4.6.2 以上，Windows 上掛 IIS 或 Windows Service）；(b) 的 `ILogger` 那條**不支援 Framework**（官方文件明寫），log4net、NLog 5 以上有另一條 bytecode 的路（Experimental，沒驗過）。細節在 [pdca/phase1-collect/2026-10-ingest-keys-check.md](../pdca/phase1-collect/2026-10-ingest-keys-check.md) 的 Act。
- **trace ↔ metrics：** 都有 `resource.attributes.service.name`、`k8s.namespace.name`、`k8s.pod.name`。

## 欄位

用 OTel 原生的格式存（ES 9 內建的 `traces-otel@template`，logsdb 模式）：

| 欄位 | 內容 |
|---|---|
| `trace_id`、`span_id`、`parent_span_id` | 請求與這一段的 ID |
| `name`、`kind`、`duration`、`status.code` | 這一段做什麼、花多久、成功與否 |
| `attributes.*` | 這一段的細節，例如 `http.request.method`、`http.response.status_code` |
| `resource.attributes.service.name` | 應用自己設的（chart 設成 `app.kubernetes.io/name`） |
| `resource.attributes.service.version`、`.service.namespace` | Pod label，跟 log、metrics 一樣 |
| `resource.attributes.group`、`.dept` | 分流結果、namespace 的 dept |
| `resource.attributes.k8s.*` | namespace、Pod、節點、Deployment、cluster |

pod label、`container.*` 不會帶進 trace（只給 log 用）。

## 部署

中央 ES 和每個 cluster 的步驟跟 log、metrics 一樣（[log/README.md 的「部署」](../log/README.md#部署)）：重跑 `elasticsearch-setup.sh`，新發的 key 才有 `traces-*.otel-*` 的權限（或用 `rotate-writer-key.sh` 一個一個 group 換，不掉資料）；`kubectl apply -k telemetry/collectors` 會多一個 Service `otel-node-collector`、StatefulSet `trace-sampler`（尾端取樣）。

ES 要有 `/_otlp/v1/traces`（測試用 9.5.5）：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -u elastic:<password> -X POST -H "Content-Type: application/x-protobuf" https://<es>:9200/_otlp/v1/traces
```

## 驗證

```bash
ES_URL=http://<ip>:9200 e2e/local-k3s/run-trace-e2e.sh
```

測試應用（[trace-app.sh](../../e2e/local-k3s/trace-app.sh)）每 5 秒印一行帶 `trace_id` 的 log、送一個同 `trace_id` 的 span。it-mes-prod 那個故意在資源欄位自稱 imc-wip-prod、dept D1110、group imc、假的 Pod UID。**23 項全部通過**：

| 類別 | 檢查 |
|---|---|
| 分流 | 三個 namespace 的 trace 各進自己的 group，沒有落錯 |
| 擋冒充 | 自稱 IMC 的那個仍進 IT，namespace、dept、Pod UID 都是真的；IMC 裡沒有它的資料 |
| 欄位 | 服務名稱、版本、cluster、Pod 名稱；沒有夾帶 pod label |
| 串起來 | 從 trace 的 `trace_id` 找得到那行 log，反過來也找得到 |
| 去重複 | 同一個 span 送 3 次，ES 只有 1 筆（另外兩次回 409） |
| 權限 | 寫入 key 能寫所有 group 的 trace、不能讀（2026-10 起一把共用）；AIOps key 能讀全部；IT 的唯讀角色看得到 IT 的 trace |

另外手動測過（每個測試應用每 5 秒一個 span）：

| 情境 | 結果 |
|---|---|
| ES 正常關機 3 分鐘 | 停機期間三個應用各 35～36 個 span 全部補進來，沒有重複 |
| ES `kill -9` 兩次（第 2 次在補送中） | 各 20 個 span 全部補進來，沒有重複 |

## 坑

集中在 [troubleshooting.md](../troubleshooting.md#trace)。

## 還沒做

1. **保留天數**：已套用（[retention.md](../retention.md)），各 group 要不要不同還要確認。
2. **Kibana 的 APM 畫面**：資料是 OTel 原生格式，Kibana 的 Observability 應該能直接看，還沒確認。

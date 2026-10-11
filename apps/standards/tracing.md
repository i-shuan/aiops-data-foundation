# Trace 埋點指引（給應用團隊）

一句話：**Java 服務用 app-common chart 打開 Java agent，大部分 trace 就自動有了，不用改程式。** 只有想看業務步驟、帶業務欄位，或用到 agent 不認得的元件時，才需要自己加幾行。

平台端怎麼收、存在哪裡，見 [docs/traces.md](../../docs/traces.md)。

## 1. 先打開自動埋點（必做，零程式碼）

values.yaml：

```yaml
otel:
  javaAgent:
    enabled: true
    image: nexus.aillm.spil.com.tw/docker/open-telemetry/autoinstrumentation-java:2.10.0
  # 不用調：預設全部送，平台的 trace-sampler 在請求結束後決定留不留（錯的、慢的全留）
  # traces:
  #   sampleRatio: 1.0
```

打開之後，下面這些**自動**記錄，也會自動把 `trace_id` 傳給下游服務、寫進 log（搭配 [logback-spring.xml](java/logback-spring.xml)）：

| 類型 | 例子 |
|---|---|
| 收到的 HTTP 請求 | Spring MVC、Spring WebFlux、Tomcat、Undertow |
| 發出的 HTTP 呼叫 | RestTemplate、WebClient、OkHttp、Apache HttpClient、Feign |
| 資料庫 | JDBC（Oracle、MySQL、PostgreSQL、SQL Server）、Hibernate、MyBatis（經 JDBC） |
| 快取、訊息 | Redis（Lettuce、Jedis）、Kafka、RabbitMQ、JMS |
| 排程與非同步 | `@Scheduled`、`@Async`、Java 標準的 `ExecutorService` |
| 例外 | 沒接住、往外拋的例外：記下類別、訊息、stack trace，那一段標成錯誤 |

**預設不收**：HTTP body 與 header、SQL 裡的實際值（換成 `?`）、使用者資料。

### 確認有沒有收到

在 Kibana 的 Discover 選 `traces-*.otel-*`，查：

```
resource.attributes.service.name : "<你的服務名稱>"
```

或拿一筆 log 的 `trace.id`，查 `trace_id : "<那個值>"`，就能看到那個請求完整的路徑。

## 2. 什麼時候要自己加

| 情況 | 例子 | 做法 |
|---|---|---|
| 想看業務步驟花多久 | 派工、批號驗證、出站各一段 | [加 `@WithSpan`](#2-1-業務步驟withspan) |
| 想在 trace 上帶業務欄位 | 批號、機台、工單 | [加 attribute](#2-2-帶業務欄位) |
| agent 不認得的元件 | 自家框架、SECS／HSMS、自訂 TCP 協定 | [自己開一段](#2-3-agent-不認得的元件) |
| 自己管理執行緒 | 自己 `new Thread`、自訂佇列 | [把 context 帶過去](#2-4-自己管理的執行緒) |

只用到第 1 節列的元件，而且不需要業務步驟的話，**不用做這一節**。

### 依賴

只需要 API（agent 會提供實作），版本跟 agent 對齊：

```xml
<dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>io.opentelemetry.instrumentation</groupId>
      <artifactId>opentelemetry-instrumentation-bom</artifactId>
      <version>2.10.0</version>   <!-- 跟 chart 的 otel.javaAgent.image 同一版 -->
      <type>pom</type>
      <scope>import</scope>
    </dependency>
  </dependencies>
</dependencyManagement>

<dependencies>
  <!-- @WithSpan、@SpanAttribute -->
  <dependency>
    <groupId>io.opentelemetry.instrumentation</groupId>
    <artifactId>opentelemetry-instrumentation-annotations</artifactId>
  </dependency>
  <!-- Span、Tracer（只有 2-2、2-3、2-4 需要） -->
  <dependency>
    <groupId>io.opentelemetry</groupId>
    <artifactId>opentelemetry-api</artifactId>
  </dependency>
</dependencies>
```

沒掛 agent 時（例如本機跑單元測試），這些 API 什麼都不做，不會出錯。

### 2-1. 業務步驟：`@WithSpan`

```java
import io.opentelemetry.instrumentation.annotations.SpanAttribute;
import io.opentelemetry.instrumentation.annotations.WithSpan;

@WithSpan("dispatch lot")
public DispatchResult dispatch(@SpanAttribute("lot.id") String lotId, String equipmentId) {
    ...
}
```

呼叫這個方法就會多一段「dispatch lot」，掛在目前請求底下，花多久、有沒有拋例外都會記。

### 2-2. 帶業務欄位

在已經有的那一段（例如自動產生的 HTTP 那段）加欄位：

```java
import io.opentelemetry.api.trace.Span;

Span.current().setAttribute("lot.id", lotId);
Span.current().setAttribute("equipment.id", equipmentId);
```

### 2-3. agent 不認得的元件

例如 EAP 送 SECS 訊息給機台：

```java
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.trace.*;
import io.opentelemetry.context.Scope;

private static final Tracer tracer = GlobalOpenTelemetry.getTracer("eap");

public Reply send(SecsMessage msg) {
    Span span = tracer.spanBuilder("SECS " + msg.stream() + "F" + msg.function())   // 例如 SECS S6F11
            .setSpanKind(SpanKind.CLIENT)
            .setAttribute("equipment.id", msg.equipmentId())
            .startSpan();
    try (Scope ignored = span.makeCurrent()) {
        return secsClient.send(msg);
    } catch (Exception e) {
        span.recordException(e);
        span.setStatus(StatusCode.ERROR);
        throw e;
    } finally {
        span.end();      // 一定要 end，不然這一段不會送出
    }
}
```

**機台不會把 `trace_id` 傳回來**，所以 trace 只會到 EAP 為止。這一段的 log 一定要寫出 `trace_id`（照 logback-spring.xml 就會有），之後才能從機台事件對回是哪個請求。

自家的 HTTP／訊息協定如果可以帶 header，把 `traceparent` 帶過去，下游就接得起來：

```java
GlobalOpenTelemetry.getPropagators().getTextMapPropagator()
    .inject(Context.current(), headers, (h, k, v) -> h.put(k, v));
```

### 2-4. 自己管理的執行緒

用 Spring 的 `@Async` 或標準 `ExecutorService` 的話，agent 會自動處理。自己 `new Thread` 或丟進自訂佇列時，把 context 包起來：

```java
import io.opentelemetry.context.Context;

new Thread(Context.current().wrap(() -> process(lot))).start();
```

## 3. 規則

- **段的名稱不能帶 ID。** 用 `dispatch lot`，不要 `dispatch lot L1919`。名稱是拿來分群統計的，帶了 ID 每一筆都不一樣，AIOps 就算不出「這個步驟平常多快」。ID 放 attribute。
- **attribute 名稱**：OpenTelemetry 已有標準名稱的用標準的（`http.*`、`db.*`、`messaging.*`）；我們自己的業務欄位**已定案**，統一用：

  | 欄位 | 意思 |
  |---|---|
  | `lot.id` | 批號 |
  | `equipment.id` | 機台編號 |
  | `work_order.id` | 工單 |
  | `recipe.id` | 配方 |

  要新增請先跟平台確認，全廠用同一個名稱 AIOps 才能跨服務比對。
- **不要放敏感資料**：帳號、密碼、token、個資。
- **不要每個迴圈項目開一段**。一次處理 1,000 筆就一段，把筆數放 attribute（`batch.size`）。段太多會讓 trace 很大、很難看。
- **不要自己設 `k8s.*`、`dept`、`group`**：平台會拿掉，改依 Pod 判斷。
- **錯誤要讓它標成錯誤**：例外往外拋，agent 會記；自己接住處理掉、但這次請求算失敗的，呼叫 `Span.current().setStatus(StatusCode.ERROR)`。

## 4. 取樣

**應用不用取樣，保持 `otel.traces.sampleRatio: 1.0`（預設）全部送。** 平台的 trace-sampler 等整個請求結束後才決定留不留：錯誤、5xx、丟過例外、超過 1 秒的全留，正常的隨機留 5%（[docs/traces.md](../../docs/traces.md)、[docs/aiops-trace.md](../../docs/aiops-trace.md#橫跨所有場景的前提取樣)）。

在應用裡調低（例如 `0.1`）的話，請求一開始就決定丟掉，那時還不知道它會不會出事：**出事的請求也只剩一成**，根因分析就是碰運氣。只有量大到 node Collector 收不動時才考慮，先跟平台確認。

## 5. 不是 Java 的服務

chart 目前只會自動掛 Java agent。其他語言用該語言的 OpenTelemetry SDK 或 agent，設好這幾個環境變數就會送到平台：

| 環境變數 | 值 |
|---|---|
| `OTEL_SERVICE_NAME` | 服務名稱（跟 `app.kubernetes.io/name` 一樣） |
| `OTEL_TRACES_EXPORTER` | `otlp` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | `http://otel-node-collector.otel-collector.svc:4318/v1/traces` |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | `http/protobuf` |
| `OTEL_TRACES_SAMPLER`、`OTEL_TRACES_SAMPLER_ARG` | `parentbased_traceidratio`、取樣比例 |

| 語言 | 自動埋點 |
|---|---|
| .NET、Python、Node.js | 有官方的自動埋點，掛上去就有 HTTP、資料庫等 |
| Go | 基本上要改程式：用 SDK 包 HTTP server／client 與資料庫 |
| C／C++ | 要自己埋 |

送出來的連線一定要是 Pod 自己的 IP（不要用 hostNetwork），平台才判斷得出屬於哪個部門。

# 欄位統一：log、metrics、trace 用同一套名字（2026-10）

**狀態**：進行中（只有 Plan，還沒動工）　　**日期**：2026-10-25　　**相關**：分支 `claude/field-unify`
**模組**：② 清資料（進來時統一格式）。連帶簡化 ④ 判斷、⑦ 根因分析的設定。

## Plan：這一次要達到什麼

- **需求**：同一個東西在三種資料裡有不同名字。「服務名稱」log 叫 `service.name`、metrics 和 trace 叫 `resource.attributes.service.name`；「HTTP 請求時間」Java 叫 `http_server_request_duration_seconds`、.NET 叫 `http.server.request.duration`。每支查資料的程式（baseline、品質檢查、根因分析）都要自己記兩套名字，`series.yaml` 每條序列都寫兩份（`variants`）。
- **目的**：**名字的差異在資料進來時（ingestion）就處理掉，查的人只看到一套。** 做完之後 `series.yaml` 的 `variants` 全部拿掉，log 的查詢條件改成跟 metrics、trace 同一條路徑。
- **範圍**：(1) log 的身分欄位補 OTel 路徑；(2) Java 的 HTTP 指標在 Collector 改成 OTel 名稱。不做：業務 ID（等名稱定案、log 樣本到）。

### 原則：在 ingestion pipeline 處理，不在查詢層處理

欄位不一致有兩個地方可以修：

| 修在哪 | 做法 | 問題 |
|---|---|---|
| 查詢層（現況） | 每支程式自己寫「log 用 A 路徑、metrics 用 B 路徑」；`series.yaml` 用 `variants` 每條寫兩份 | 名字散在每支程式、每個設定檔；多一種語言、多一支程式都要再寫一次；漏寫就是查不到資料、沒有錯誤訊息 |
| **ingestion pipeline**（要改成這樣） | 資料進來時就改成同一套名字：log 在 ES 的 ingest pipeline 補欄位，metrics 在 Collector 改名 | 對照表只寫一次，在一個地方；下游（baseline、quality、RCA、人用 Kibana）全部只看到一套 |

對照表**不會隨服務數量長大**：它跟「有幾種埋點工具」成正比。一千個 Java 服務用同一支 agent，就是一組對照；多一種語言（Go、Python）才多一組。

### (1) log 的身分欄位：ES ingest pipeline 補 OTel 路徑

log 進 ES 時走中央 pipeline（`platform/central/elasticsearch/pipeline.json`），在這裡把 ECS 名字的值**同時放一份**到 OTel 路徑（`set` + `copy_from`，不是改名：原本的欄位留著，Kibana 現有的查詢不會壞）。

| log 現在的欄位（ECS） | 補到（OTel，跟 metrics、trace 一樣） |
|---|---|
| `service.name` | `resource.attributes.service.name` |
| `kubernetes.namespace` | `resource.attributes.k8s.namespace.name` |
| `kubernetes.pod.name` | `resource.attributes.k8s.pod.name` |
| `kubernetes.container.name` | `resource.attributes.k8s.container.name` |
| `kubernetes.node.name` | `resource.attributes.k8s.node.name` |
| `orchestrator.cluster.name` | `resource.attributes.k8s.cluster.name` |
| `trace.id`、`span.id` | `trace_id`、`span_id`（trace 資料裡的名字） |

兩種做法要選一個（Do 的時候實測決定）：

| | ES `alias` 欄位型別 | pipeline 多存一份 |
|---|---|---|
| 怎麼做 | mapping 裡宣告 `resource.attributes.service.name` 是 `service.name` 的別名 | pipeline `set` 一個新欄位 |
| 空間 | 不佔（只是另一個名字） | 每筆多幾個 keyword |
| 限制 | alias 不能跨 index 指到不存在的欄位；`terms` 聚合、`exists` 查詢要確認都支援 | 無 |
| 偏好 | **先試這個** | alias 不行再用 |

### (2) Java 的 HTTP 指標：Collector 進來時改成 OTel 名稱

Java agent 用 Prometheus 格式開給 Collector 抓，名字是 Prometheus 慣例；.NET 用 OTLP 推，名字是 OTel 慣例。在 node Collector 抓 Prometheus 那條 pipeline（`node.yaml` 的 `metrics` pipeline）加一個 `transform`：

| Java（Prometheus 格式） | 改成（OTel；.NET 本來就是這個） |
|---|---|
| `http_server_request_duration_seconds` | `http.server.request.duration` |
| `attributes.http_route` | `attributes.http.route` |
| `attributes.http_request_method` | `attributes.http.request.method` |
| `attributes.http_response_status_code`（字串 `"500"`） | `attributes.http.response.status_code`（數字 500） |

做完 ES 裡只有一種名字，`series.yaml`、`quality.yaml` 的 `variants` 和兩份 `filter` 全部拿掉。

**要實測的風險**：Prometheus 格式轉過來的直方圖，bucket 邊界可能跟 .NET OTLP 的不一樣；同一個 TSDS 裡兩種 bucket 混在一起，p95 怎麼算、準不準，要用兩種來源各送一批已知分布的資料比對。

### (3) metrics 跟 trace 怎麼連

**metrics 裡沒有 trace_id**（exemplar 不存，[2026-10-metrics-to-trace.md](2026-10-metrics-to-trace.md) 已決定），所以 metrics ↔ trace 不是「同一筆資料對同一筆」，是**用共同的鑰匙縮範圍**：

```
metrics：order-service 的 GET /checkout，10:05～10:20 錯誤率飆高
   │  鑰匙：服務名稱 ＋ 路由 ＋ 時間窗（＋ Pod，如果報的是單一 Pod）
   ▼
trace：撈這段時間 order-service /checkout 的 trace
        出事時：最慢 200 個 ＋ 最多 100 個出錯的（尾端取樣把慢的、錯的全留了）
        平常：  一週前同一時段
        → 統計共同卡在哪一段、錯從哪個下游開始
   │  鑰匙：trace_id（log 裡是 trace.id，agent 自動寫進 MDC／scope）
   ▼
log：那些 trace 的 log，看錯誤訊息、例外
```

這就是為什麼 (1)(2) 要先做：**鑰匙的名字三種資料要一樣**（服務名稱、路由、Pod），否則每一步都要翻譯。鑰匙對照表：

| 鑰匙 | metrics | trace | log（現況 → 補成） |
|---|---|---|---|
| 服務 | `resource.attributes.service.name` | 同左 | `service.name` → 同左 |
| 路由 | `attributes.http.route`（Java 改名後） | span 的 `attributes.http.route` | log 沒有路由（REST API 解析規則做了才有） |
| Pod、namespace | `resource.attributes.k8s.pod.name` 等 | 同左 | `kubernetes.pod.name` → 同左 |
| 時間 | 異常的時間窗 | span `@timestamp` | log `@timestamp`（已對齊，[2026-10-time-alignment.md](2026-10-time-alignment.md)） |
| 請求 | — | `trace_id` | `trace.id` → `trace_id` |

### (4) 業務 ID（交易 ID）——這次不做，先記設計

- **不放 metrics**（鐵則）：每個不同的值就是一條新序列，交易 ID 每筆都不同，序列數會爆炸。所以 metrics 跟交易 ID 永遠不直接接，走 (3) 的路徑：metrics → trace → 交易 ID。
- **新程式**：入口把交易 ID 放 span attribute，同時放 log 的 MDC／scope。trace、log 都直接帶。
- **舊程式不改**：log 裡本來就印 `txnId=…` 這類字串，中央 pipeline 依格式抽成標準欄位；再靠 log 的 `trace.id` 對到 trace。
- **卡住的**：名稱沒定（`apps/standards/tracing.md` 已定案的只有 `lot.id`、`equipment.id`、`work_order.id`、`recipe.id`）；抽取規則要兩三個服務的真實 log 樣本。

## Do：改動哪些 code、改了什麼、為何要改

（還沒動工）預計：`component-mappings.json` 或 `pipeline.json`（log 補欄位）、`node.yaml`（Java 指標改名）、`series.yaml`／`quality.yaml`（拿掉 `variants`）、`docs/log/README.md`、`docs/metrics/README.md`、`docs/rca/README.md` 的鑰匙表。

## Check：測試的目的與結果

（還沒動工）預計：
- log 補的欄位用 `terms` 聚合、`exists` 查詢各查一次，跟原欄位結果一樣。
- Java、.NET 各送一批已知分布的請求時間，比 p95 差多少。
- `series.yaml` 拿掉 `variants` 之後，baseline 算出來的序列數、數值跟拿掉前一樣。

## Act：Check 後沒改善的、還沒解決的

（還沒動工）

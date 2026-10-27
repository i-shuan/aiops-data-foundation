# 欄位統一：log、metrics、trace 用同一套名字（2026-10）

**狀態**：已完成（k3s 端對端待下次 e2e 跑）　　**日期**：2026-10-25　　**相關**：分支 `claude/field-unify`
**模組**：② 清資料（進來時統一格式）；連帶簡化 ④ 判斷、⑦ 根因分析的設定

## Plan：這一次要達到什麼

- **需求**：同一個東西在三種資料裡名字不同，每支查資料的程式（baseline、品質檢查、根因分析）都要記兩套名字，`series.yaml` 每條序列寫兩份（`variants`）。
  - 服務名稱：log 叫 `service.name`，metrics、trace 叫 `resource.attributes.service.name`。
  - HTTP 請求時間：Java 叫 `http_server_request_duration_seconds`，.NET 叫 `http.server.request.duration`。
- **目的**：**名字的差異在資料進來時（ingestion）就處理掉，查的人只看到一套。** `series.yaml` 的 `variants` 全部拿掉，log 的查詢條件跟 metrics、trace 同一條路徑。
- **範圍**：(1) log 的身分欄位補 OTel 路徑；(2) Java 的 HTTP 指標在 Collector 改成 OTel 名稱。不做業務 ID（等名稱定案、log 樣本到）。

### 原則：在 ingestion 處理，不在查詢層處理

| 修在哪 | 做法 | 結果 |
|---|---|---|
| 查詢層（舊） | 每支程式自己記「log 用 A 路徑、metrics 用 B 路徑」；`series.yaml` 用 `variants` | 名字散在每支程式、每個設定檔 |
| **ingestion（採用）** | log 在 ES 補欄位，metrics 在 Collector 改名 | 對照表只寫一次；下游（baseline、quality、RCA、Kibana）只看到一套 |

- 查詢層的做法：多一種語言、多一支程式都要再寫一次；漏寫就是查不到資料，沒有錯誤訊息。
- 對照表**不會隨服務數量長大**，跟「有幾種埋點工具」成正比：一千個 Java 服務用同一支 agent 就是一組對照；多一種語言（Go、Python）才多一組。

### (1) log 的身分欄位：ES mapping 用 alias 補 OTel 路徑

| log 現在的欄位（ECS） | 補到（OTel，跟 metrics、trace 一樣） |
|---|---|
| `service.name` | `resource.attributes.service.name` |
| `kubernetes.namespace` | `resource.attributes.k8s.namespace.name` |
| `kubernetes.pod.name` | `resource.attributes.k8s.pod.name` |
| `kubernetes.container.name` | `resource.attributes.k8s.container.name` |
| `kubernetes.node.name` | `resource.attributes.k8s.node.name` |
| `orchestrator.cluster.name` | `resource.attributes.k8s.cluster.name` |
| `trace.id`、`span.id` | `trace_id`、`span_id`（trace 資料裡的名字） |

| | ES `alias` 欄位型別（**採用**） | pipeline 多存一份 |
|---|---|---|
| 怎麼做 | mapping 宣告 OTel 路徑是 ECS 欄位的別名 | pipeline `set` 一個新欄位 |
| 空間 | 不佔（只是另一個名字） | 每筆多幾個 keyword |
| 原欄位 | 留著，Kibana 現有查詢不壞 | 留著 |
| 限制 | 不在 `_source`，程式要用 `fields` 取 | 無 |

### (2) Java 的 HTTP 指標：Collector 進來時改成 OTel 名稱

Java agent 用 Prometheus 格式開給 Collector 抓（Prometheus 慣例），.NET 用 OTLP 推（OTel 慣例）。在 node Collector 抓 Prometheus 那條 pipeline（`node.yaml` 的 `metrics`）改名：

| Java（Prometheus 格式） | 改成（OTel；.NET 本來就是這個） |
|---|---|
| `http_server_request_duration_seconds` | `http.server.request.duration` |
| `attributes.http_route` | `attributes.http.route` |
| `attributes.http_request_method` | `attributes.http.request.method` |
| `attributes.http_response_status_code`（字串 `"500"`） | `attributes.http.response.status_code`（數字 500） |

### (3) metrics 跟 trace 怎麼連

**metrics 裡沒有 trace_id**（exemplar 不存，[rca/README.md「為什麼不用 exemplar」](../../rca/README.md#為什麼不用-exemplar用服務路由時間撈一批) 已決定），所以 metrics ↔ trace 是**用共同的鑰匙縮範圍**，不是同一筆對同一筆：

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

鑰匙的名字三種資料要一樣（服務名稱、路由、Pod），否則每一步都要翻譯；所以 (1)(2) 要先做。

| 鑰匙 | metrics | trace | log（現況 → 補成） |
|---|---|---|---|
| 服務 | `resource.attributes.service.name` | 同左 | `service.name` → 同左 |
| 路由 | `attributes.http.route`（Java 改名後） | span 的 `attributes.http.route` | 沒有（REST API 解析規則做了才有） |
| Pod、namespace | `resource.attributes.k8s.pod.name` 等 | 同左 | `kubernetes.pod.name` → 同左 |
| 時間 | 異常的時間窗 | span `@timestamp` | log `@timestamp`（已對齊，[2026-10-time-alignment.md](2026-10-time-alignment.md)） |
| 請求 | — | `trace_id` | `trace.id` → `trace_id` |

### (4) 業務 ID（交易 ID）：這次不做，先記設計

- **不放 metrics**（鐵則）：每個不同的值就是一條新序列，交易 ID 每筆都不同，序列數會爆炸。metrics 跟交易 ID 永遠不直接接，走 (3)：metrics → trace → 交易 ID。
- **新程式**：入口把交易 ID 放 span attribute，同時放 log 的 MDC／scope。
- **舊程式不改**：log 裡本來就印 `txnId=…` 這類字串，中央 pipeline 依格式抽成標準欄位；再靠 log 的 `trace.id` 對到 trace。
- **卡住的**：名稱沒定（`sdk/standards/tracing.md` 已定案的只有 `lot.id`、`equipment.id`、`work_order.id`、`recipe.id`）；抽取規則要兩三個服務的真實 log 樣本。

## Do：改動哪些 code、改了什麼、為何要改

**決定**：正式環境還沒收 Java 的 metrics，沒有舊名字的歷史資料，`variants` 直接拿掉，不留過渡期。

| 檔案 | 改了什麼 | 為何要改 |
|---|---|---|
| `telemetry/collectors/node.yaml` | Prometheus 的 metrics pipeline 加 `metricstransform/java-http`：指標改名 | ES 裡只剩 OTel 名字，下游不用再分語言 |
| 同上 | 加 `transform/java-http`：`http_route`、`http_request_method` 改點號；狀態碼字串 → 數字 | 同上 |
| 同上 | `error_type`、`network_protocol_version`、`url_scheme` 同樣改點號；刪舊 key | 同上 |
| 同上 | 兩個處理器放在 `cumulativetodelta` 之前；條件只對舊名字生效 | Java agent 升版自己吐 OTel 名字也不會改錯；多一種語言在這裡多一組 |
| `telemetry/storage/component-mappings.json` | 加 `alias`：`resource.attributes.service.name` | log 用跟 metrics、trace 同一條路徑查 |
| 同上 | 加 `alias`：`resource.attributes.k8s.{namespace,pod,container,node,cluster}.name` | 同上 |
| 同上 | 加 `alias`：`trace_id`、`span_id`；都指到原本的 ECS 欄位 | 同上；選 alias 的理由見 Plan (1) |
| `engine/configs/baseline/series.yaml` | `http.requests`、`http.error_rate`、`http.latency_p95_s` 從兩份 `variants` 改成一份 OTel 名字 | 名字統一後不需要分語言 |
| 同上 | `log.lines`、`log.errors` 的 `by` 改 `resource.attributes.service.name` | log 的鑰匙跟 metrics 同路徑 |
| `engine/configs/baseline/quality.yaml` | `route_cardinality` 和 `_dotnet` 合成一個；`metrics_*` 的雙名字 filter 改單一 | 名字統一 |
| 同上 | log 檢查的 `by`、`fields` 改 OTel 路徑（`trace_id`、`resource.attributes.k8s.pod.name`…） | 同上 |
| 同上 | 新增 `quality.metric_name_legacy`（type `present`）：ES 裡又出現舊名字就報 | 舊名字代表 Collector 沒改名，那些服務 baseline、RCA 會默默看不到 |
| `engine/configs/baseline/peer.yaml`、`chronic.yaml` | 拿掉 `variants`；`by` 改 OTel 路徑 | 同上 |
| `engine/detector/es.py`、`peer.py`、`__main__.py` | 刪 `variants` 的分支；`__main__` 加 `present` 類型 | 程式少一條路徑 |
| 同上 | `fetch` 保留「有 `keys` 就把 `by` 的欄位改名成 keys」 | quality、peer 仍用短名字 |
| `engine/detector/quality.py` | 加 `eval_present` | `metric_name_legacy` 用 |
| `engine/detector/adjust.py` | `ALIASES` 拿掉 ECS 名字和底線名字，只剩「短名字 ↔ OTel 路徑」 | 不再有別的寫法要對 |
| `engine/investigator/es.py`、`__main__.py` | log 查詢改用 `resource.attributes.service.name`、`trace_id`；取值從 `_source` 改 `fields` | 三種資料同一條路徑；alias 不在 `_source` |
| 同上 | 路由鑰匙改 `attributes.http.route`、`http.request.method` | 同上 |
| `engine/tests/*` | 依新名字改；加 `Present` 測試 | — |
| `e2e/local-k3s/run-metrics-e2e.sh`、`run-rca-e2e.sh` | ES 裡查的欄位名改 OTel；狀態碼從 `prefix "5"` 改 `range gte 500` | Collector 改名後 ES 裡就是新名字、數字 |
| `docs/log/README.md`、`metrics/README.md`、`metrics/catalog.md`、`metrics/dotnet.md` | 欄位名 | — |
| `docs/rca/README.md`、`engine/configs/baseline/README.md` | ② 兩列改 ✅；鑰匙表加路由列、標 alias | — |

`quality.metric_name_legacy` 會報的情況：Collector 設定漏了、新 cluster 沒更新、agent 吐了對照表沒列的名字。

## Check：測試的目的與結果

| 測試目的（為了確認什麼） | 怎麼測 | 結果 |
|---|---|---|
| Collector 的改名寫法對不對 | docker：假的 Prometheus 端點吐 Java agent 格式（見下）；Collector 0.162.0；debug exporter 看輸出 | 名字 `http.server.request.duration`；舊 key 都刪掉 |
| 屬性、狀態碼型別 | 同上 | `http.request.method=GET`、`http.route=/orders`、`http.response.status_code=Int(200)` |
| 其他屬性 | 同上 | `error.type`、`network.protocol.version`、`url.scheme` 都改點號 |
| 直方圖、其他指標有沒有被動到 | 同上 | 14 個 bucket 邊界原樣；`jvm_memory_used_bytes` 不受影響 |
| ES alias：聚合、存在查詢 | es-t：用改過的 `component-mappings.json` 建 index，寫 3 筆 ECS 格式的 log | `terms`（service、namespace）對；`exists trace_id` 2 筆對；`must_not exists pod` 0 筆對 |
| ES alias：quality、RCA 的查法 | 同上 | `composite`（含 `missing_bucket`）對；RCA 的 `terms trace_id` + `fields` 取值對 |
| 跟沒有 alias 的舊 index 一起查 | 同上 | 不會錯，舊 index 那部分沒值而已 |
| alias 在不在 `_source` | 同上 | 不在（所以 RCA 改用 `fields`） |
| 拿掉 `variants` 後程式的行為 | 單元測試（`/usr/bin/python3.13 -m pytest`） | 196 個全過，重點見下 |
| Java、.NET 的 p95 會不會算出兩種結果 | 讀測試應用吐的 bucket 邊界，對照 OTel SDK 預設 | 邊界相同（見下）；**推論** p95 一致，沒實際比對 |

- 假的 Prometheus 端點：`http_server_request_duration_seconds`（跟 `e2e/local-k3s/http-metrics-app.yaml` 同一份）＋ `jvm_memory_used_bytes`，跑 `metricstransform/java-http` + `transform/java-http`。
- 單元測試重點：`test_peer` 的 `volume_spec` 回單一 `hist_count`；`test_values` 的 `hist_ratio` 用 `range gte 500`。
- `test_adjust`、`test_chronic`、`test_sources` 的 service 鑰匙改 `resource.attributes.service.name` 後 match 得到；`Present`：12 筆報、0 筆不報。
- bucket 邊界：Java agent 的 14 個邊界 `[0.005 … 10]` 就是 OTel SDK 對 `http.server.request.duration` 的預設，.NET 自動埋點同一個 SDK 預設；ES 兩邊都轉成 `exponential_histogram`。

**沒測到**：k3s 端對端（Collector 改名 → ES → baseline 跑出序列）；`quality.metric_name_legacy` 對真的舊資料報不報（只有單元測試）；alias 在 Kibana Discover 的顯示。

## Act：Check 後沒改善的、還沒解決的

| 沒改善的 | 在什麼情況下發現的 | 影響 | 後續 |
|---|---|---|---|
| alias 不在 `_source` | 改 RCA 時 | 新程式用 `_source` 讀 `resource.attributes.*` 會拿到空值 | 寫進 log/README；程式一律用 `fields` |
| Java、.NET 的 p95 一致性是推論 | Check | bucket 不同的話，同一個服務換語言時 baseline 會跳 | 下次 k3s e2e 用 `http-metrics-app`、.NET 測試應用各送已知分布比一次 |
| 只處理 HTTP server 這組；`http.client.request.duration`、JVM 指標還是 Prometheus 名字 | 寫對照表時 | 目前沒有序列用到；用到時又要分語言 | 用到哪一組就在 `metricstransform/java-http` 加一條 |
| 未來會改名的情況（見下） | Plan 討論時 | 語意規範改版時要全面改名，兩種語言不會同一天升 | 升版前看 agent 的 release note |
| `metric_name_legacy` 只盯一個舊名字 | 同上 | 對照表沒列的新名字不會報 | 加「沒列在對照表、但像 HTTP 直方圖的名字」的檢查 |
| [⑧] log 沒有 REST API 路由欄位 | 寫鑰匙表時 | metrics 報某條路由出事時，log 只能用服務＋時間撈 | 等 REST API 解析規則（要 log 樣本） |

- JVM 跟 .NET runtime 本來就是不同指標，不用對。
- 未來會改名的情況（上線後要盯）：
  1. Java agent 升大版改用 OTel 名字開 Prometheus：規則打不到、資料直接過，`metric_name_legacy` 不會報，沒影響。
  2. OTel 語意規範改版（上次 2023 年 `http.server.duration` → `http.server.request.duration`）：Java、.NET 升版都會跟著變，Collector 對照表、`series.yaml` 都要改名。
  3. 新語言（Go、Python）用 Prometheus 格式進來：多一組對照。
- **決定**：欄位差異一律在 ingestion 處理（log 在 ES mapping 用 alias、metrics 在 Collector 改名），查詢層不再翻譯；`variants` 刪掉不留過渡。
- **下一輪**：k3s e2e 跑一次（`run-metrics-e2e.sh`、`run-rca-e2e.sh` 已改成新名字）；業務 ID 等名稱定案和 log 樣本。

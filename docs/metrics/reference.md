# Metrics 進 Elasticsearch：規格

> **類型：規格 Reference**　同主題：[原理](README.md)・[操作（部署、驗證）](how-to.md)・[指標目錄（規格）](catalog.md)・[metrics 能做什麼（原理）](capabilities.md)・[案例與實測](cases.md)・[.NET 原理](dotnet.md)・[.NET 操作](dotnet-how-to.md)・[.NET 規格](dotnet-reference.md)

檔案位置與 ES 裡的欄位。為什麼這樣設計見 [README.md](README.md)，怎麼做見 [how-to.md](how-to.md)。

## 檔案

| 檔案 | 用途 |
|---|---|
| [telemetry/collectors/common.yaml](../../telemetry/collectors/common.yaml) | 兩種 Collector 共用：邊緣身份鑑別與標籤注入 (Edge Identity Sanitization & Injection)、讀 dept、dept → group、每個 group 的出口 |
| [telemetry/collectors/node.yaml](../../telemetry/collectors/node.yaml) | 節點那一份收什麼：kubelet、cAdvisor（CPU 限流）、應用的 Prometheus metrics、應用用 OTLP 推的 metrics |
| [telemetry/collectors/cluster.yaml](../../telemetry/collectors/cluster.yaml) | cluster 那一份收什麼：K8s 物件的狀態 |
| [telemetry/collectors/](../../telemetry/collectors) 其餘 | 權限、DaemonSet、Deployment、kustomization |
| [telemetry/scripts/elasticsearch-setup.sh](../../telemetry/scripts/elasticsearch-setup.sh) | 跟 log 共用：寫入 key、唯讀角色、AIOps key 都涵蓋 metrics |
| [telemetry/storage/metrics-otel-custom.json](../../telemetry/storage/metrics-otel-custom.json) | TSDS 可寫入時間往後 2 小時（見「坑」：ES 重啟後拒收） |
| [e2e/local-k3s/run-metrics-e2e.sh](../../e2e/local-k3s/run-metrics-e2e.sh) | 端到端測試 |
| [e2e/local-k3s/run-metrics-scenarios.sh](../../e2e/local-k3s/run-metrics-scenarios.sh) | 情境測試：改 label、ES 停機加重啟 Collector、key 失效 |

兩種 Collector 啟動時都載入 `common.yaml` 加上自己那份，分部門的規則只有一處，不會兩邊各改各的。

## 欄位

用 OTel 原生的格式存（ES 的 OTel mapping），欄位名稱是 OpenTelemetry 的標準名稱：

| 欄位 | 內容 | 對應 log 的欄位 |
|---|---|---|
| `metrics.<名稱>` | 數值，例如 `metrics.k8s.pod.cpu.usage`、`metrics.demo_requests_total` | — |
| `attributes.*` | 這個數值自己的維度，例如應用的 `path` | — |
| `resource.attributes.service.name` | Pod label `app.kubernetes.io/name`（OTLP 推的，應用自己設的不同時被蓋掉）；沒有就用 Deployment 名稱 | `service.name` |
| `resource.attributes.service.version` | Pod label `app.kubernetes.io/version` | `service.version` |
| `resource.attributes.service.namespace` | Pod label `app.kubernetes.io/part-of` | `service.namespace` |
| `resource.attributes.group`、`.dept` | 分流結果、namespace 的 dept label | `labels.group`、`labels.dept` |
| `resource.attributes.k8s.cluster.name` | Collector 的 `CLUSTER_NAME` | `orchestrator.cluster.name` |
| `resource.attributes.k8s.namespace.name`、`.k8s.pod.name`、`.k8s.node.name`、`.k8s.deployment.name` | K8s | `kubernetes.*` |
| `data_stream.namespace` | 等於 group | 資料流名稱裡的 group |

AIOps 要把 log 和 metrics 串起來，用 `service.name` 加 `k8s.namespace.name`（log 那邊是 `kubernetes.namespace`）加時間。

**metrics 帶不了 trace_id。** OTLP metrics 的 exemplar（帶 trace_id）寫進 ES 時會被丟掉：送到 `/_otlp/v1/metrics`、或用 Collector 的 `elasticsearch` 寫入元件（otel mode）都一樣，`_source`、mapping、全文搜尋都找不到 trace_id（實測 ES 9.5.5、Collector contrib 0.162.0）。要從 metrics 找 trace，用服務、時間範圍去查。

### 應用的請求數、錯誤、延遲（AIOps baseline 用）

Java agent 會開出 `http.server.request.duration`（每個 HTTP 請求的耗時直方圖，依 method、route、狀態碼分），這是 AIOps baseline 算流量、錯誤率、延遲的來源（見 [rca/README.md「分工」](../rca/README.md#分工metrics-決定什麼時候告警trace-和-log-說明原因)）。

| | |
|---|---|
| ES 欄位 | `metrics.http.server.request.duration`，型別 `exponential_histogram`（ES 9.5 自動轉）。Java 經 Prometheus 進來的 `http_server_request_duration_seconds` 由 node Collector 改成這個名字，跟 .NET 一樣 |
| 維度 | `attributes.http.request.method`、`attributes.http.route`、`attributes.http.response.status_code`（數字） |
| 資料流 | `metrics-generic.otel-<group>`：資料帶著應用自己的 scope 名稱（例如 `io.opentelemetry.tomcat-10.0`），不是 `prometheusreceiver` |
| 每一筆是什麼 | **這 30 秒內**的請求：Collector 用 `cumulativetodelta` 把 Prometheus 的累加值換成差值（`temporality: delta`） |

**為什麼要換成差值：** Prometheus 的數字從應用啟動一直累加。直接存的話，「10:00～10:05 有幾個請求、p95 多少」要拿頭尾相減，直方圖在 ES 裡做不到；存差值就能直接加總任一段時間。實測：沒換之前 ES 也收（`temporality: cumulative`），但一加總就是錯的。

只換直方圖；計數器（counter）維持累加值，原本的資料和查法不變。

實測（測試應用每 5 分鐘 1,200 個成功、60 個 500 錯誤，成功的 10% 落在 250～500ms）：baseline 從 ES 查出 **1,260 個請求、錯誤率 4.76%、p95 0.375 秒**，跟預期完全一樣。Collector 重啟的那個 5 分鐘少了約 1/5（少抓一次）。

**代價：** Collector 重啟後，每條序列第一次抓到的值沒有「上一次」可以比，那 30 秒會少掉。應用重啟（數字歸零）會被當成重新開始，不會算出負數。

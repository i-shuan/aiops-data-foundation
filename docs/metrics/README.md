# Metrics 進 Elasticsearch

一句話：**每個 cluster 一套 OpenTelemetry Collector 收 metrics，依「每筆資料自己的 namespace」讀 dept 分 group，各 group 用自己的 key 寫進 ES 的 time series data stream。**

log 也由同一個 Collector 收（[log/README.md](../log/README.md)）：分部門的規則只有一份（`common.yaml` 的 `transform/group`，log 和 metrics 共用）、key 也共用，同一個 Pod 的 log 和 metrics 一定落在同一個 group。

## 現況與待辦（①收值、②清資料、③存）

**已完成 ✅**

| 項目 | 狀態 |
|---|---|
| node Collector：kubelet、cAdvisor（CPU 限流）、應用的指標（Java 照標註來抓、.NET 用 OTLP 推）；cluster Collector：K8s 物件狀態 | ✅ 每 30 秒 |
| 依每筆資料自己的 namespace 分部門（寫進各部門的 data stream）、一把寫入 key 所有部門共用 | ✅ |
| 直方圖從累加值換成「每 30 秒多少」，任一段時間直接加總 | ✅ |
| 外部系統：kafka_exporter | ✅ |
| 寫進 ES 的時序資料流（TSDS），重送不重複 | ✅ |

**已完成的部分，還差一點**

| 項目 | 要做的 |
|---|---|
| ② 指標名稱不一致 | ✅ node Collector 進來時把 Java（Prometheus 格式）的 `http_server_request_duration_seconds`、`http_route`、`http_request_method`、狀態碼字串改成 OTel 的 `http.server.request.duration`、`http.route`、`http.request.method`、狀態碼數字（`node.yaml` 的 `metricstransform/java-http`、`transform/java-http`）。ES 裡只有一種名字，baseline 的 `variants` 拿掉。只對舊名字生效：Java agent 升版自己吐 OTel 名字也不會改錯。`quality.metric_name_legacy` 盯「舊名字又出現」。[PDCA](../pdca/phase1-collect/2026-10-field-unify.md) |
| ① ES 剛起來回 401、Collector 直接丟 | 修在 ES 入口：健康檢查要用 API key 打 GET /（trace 線實測；metrics 用同一種 exporter，推論相同）。📝 中央 ES 前面的健康檢查要請管 ES 的人改。[PDCA](../pdca/phase1-collect/2026-10-es-startup-401.md) |
| ① .NET 還沒驗證的 | SQL Server（SqlClient）連線池指標、.NET 6、Alpine（musl）image（[metrics/dotnet.md](dotnet.md)） |
| ③ 保留天數 | ✅ 原始 14 天 → 5 分鐘一點 → 第 90 天 1 小時一點 → 第 400 天刪；降解析度後直方圖照樣保留（[retention.md](../retention.md)） |

**要做的（①收值、②清資料）**

| 項目 | 內容 | 驗收 |
|---|---|---|
| ① 主機時鐘監控（時間對齊；規則在 ④判斷） | ✅ K8s 節點：node Collector 的 `ntp` receiver（不用 node_exporter），30 分鐘量一次（receiver 的最短間隔），寫 `metrics-ntpreceiver.otel-*` 的 `metrics.ntp.offset`（奈秒）；規則 `node.clock_offset`：快慢超過 100ms warning、超過 1 秒 critical。📝 DB 主機（不在 K8s 裡）還沒有。`NTP_SERVER` 要填公司的 NTP。[PDCA](../pdca/phase1-collect/2026-10-time-alignment.md) | 實測：假 NTP 伺服器快 2.5 秒、慢 0.3 秒、差 0.05 秒，三台分別報 critical、warning、不報 |
| ② 業務 ID 不放 metrics | 交易 ID、單號只放 log、trace；放進 metrics 每個值一條新序列，序列數會爆炸 | 資料品質檢查會報序列數暴增（[rca/README.md「髒資料排除」](../rca/README.md#髒資料排除)） |

## 資料怎麼走

```
每個節點一個 Collector（DaemonSet）
   ├─ kubelet：節點、Pod、容器的 CPU、記憶體、網路、磁碟
   ├─ kubelet 的 cAdvisor：只收 CPU 被限流的兩個數字（週期數、被限流的週期數）
   ├─ 應用的 Prometheus metrics：這個節點上、Pod 標註 prometheus.io/scrape: "true" 的（Java）
   └─ 應用用 OTLP 推來的 metrics（.NET，見 dotnet.md）
整個 cluster 一個 Collector（Deployment，replicas 1）
   └─ k8s_cluster：Pod 狀態、容器重啟次數、Deployment/ReplicaSet 副本數、namespace、節點狀態
        ↓ 兩種 Collector 共用同一份規則（common.yaml）
        ↓ 1. 擋冒充：應用自帶的 dept、group 欄位一律拿掉
        ↓ 2. 依這筆資料屬於哪個 Pod／物件，讀它 namespace 的 dept label，補上服務身分
        ↓ 3. dept → group（清單同 telemetry/groups.yaml）；沒有 dept 的（例如節點本身）→ unassigned
        ↓ 4. data_stream.namespace = group；一個出口、一把寫入 key（ES_WRITER_API_KEY），用 OTLP 送到 ES 的 /_otlp/v1/metrics
        ↓    → metrics-<來源>.otel-<group>（例如 metrics-prometheusreceiver.otel-it）
        ↓ 中央 ES 9 內建的 metrics-otel@template 把它建成 time series data stream（TSDS）
   AIOps 用唯讀 key 讀 metrics-*.otel-*；人用各 group 的唯讀角色
```

`<來源>` 是 `kubeletstatsreceiver`、`k8sclusterreceiver`、`prometheusreceiver`，加上 `generic`（資料帶著應用自己的 scope 名稱的：Java agent 的指標、.NET 用 OTLP 推的），所以每個 group 有四條資料流。

## 為什麼依「每筆資料自己的 namespace」分

原本的設計（VictoriaMetrics 的 vmagent 加 `scrapeClasses`）是依「被抓取的對象在哪個 namespace」分。實測後發現兩個問題，所以改掉：

| 問題 | 實測結果 |
|---|---|
| Pod 的 CPU、記憶體是從節點的 kubelet 抓的，Pod 狀態是從 kube-system 的 kube-state-metrics 抓的 | 抓取對象不在應用的 namespace，**IT 的 Pod CPU、重啟次數全部被分到 platform**，IT 在自己的 tenant 看不到 |
| `scrapeClasses` 要讀 namespace 的 label，版本要求高 | operator 要 v0.67.0 以上、vmagent 要 v1.130.0 以上，兩個都要。舊版的 vmagent 直接起不來 |

現在的做法看的是每筆資料標的 Pod 或物件，所以 kubelet、k8s_cluster 的資料也會分到那個 Pod 的 group，而且不吃任何特定版本的功能。

## 檔案

| 檔案 | 用途 |
|---|---|
| [telemetry/collectors/common.yaml](../../telemetry/collectors/common.yaml) | 兩種 Collector 共用：擋冒充、讀 dept、dept → group、每個 group 的出口 |
| [telemetry/collectors/node.yaml](../../telemetry/collectors/node.yaml) | 節點那一份收什麼：kubelet、cAdvisor（CPU 限流）、應用的 Prometheus metrics、應用用 OTLP 推的 metrics |
| [telemetry/collectors/cluster.yaml](../../telemetry/collectors/cluster.yaml) | cluster 那一份收什麼：K8s 物件的狀態 |
| [telemetry/collectors/](../../telemetry/collectors) 其餘 | 權限、DaemonSet、Deployment、kustomization |
| [telemetry/scripts/elasticsearch-setup.sh](../../telemetry/scripts/elasticsearch-setup.sh) | 跟 log 共用：寫入 key、唯讀角色、AIOps key 都涵蓋 metrics |
| [telemetry/storage/metrics-otel-custom.json](../../telemetry/storage/metrics-otel-custom.json) | TSDS 可寫入時間往後 2 小時（見「坑」：ES 重啟後拒收） |
| [e2e/local-k3s/run-metrics-e2e.sh](../../e2e/local-k3s/run-metrics-e2e.sh) | 端到端測試 |
| [e2e/local-k3s/run-metrics-scenarios.sh](../../e2e/local-k3s/run-metrics-scenarios.sh) | 情境測試：改 label、ES 停機加重啟 Collector、key 失效 |

兩種 Collector 啟動時都載入 `common.yaml` 加上自己那份，分部門的規則只有一處，不會兩邊各改各的。

## 為什麼用 OTLP 送，不用 elasticsearch 寫入元件

OpenTelemetry Collector 有專門寫 ES 的 `elasticsearch` 寫入元件，一開始用的是它。ES 停機測試發現它會掉資料，所以改成用 OTLP 送到 ES 9 自己的 OTLP 入口（`/_otlp/v1/metrics`）：

| | `elasticsearch` 寫入元件 | OTLP 送到 ES 的 OTLP 入口（現在用的） |
|---|---|---|
| 連線逾時、沒拿到 HTTP 回應 | **當成永久錯誤，整批丟掉**，log 只有一行 `Exporting failed. Dropping data.` | 重試，直到送成功 |
| 重試設定 | 只能指定哪些 HTTP 狀態碼要重試，管不到連線錯誤 | `retry_on_failure`，`max_elapsed_time: 0` 表示一直重試 |
| ES 停 3 分鐘 | 停機期間 18 筆應用 metrics 只剩 1 筆、Pod CPU 0 筆 | 全部補齊，ES 恢復後 5 秒內補完 |
| 資料流名稱、欄位、分流 | 一樣 | 一樣 |

⚠️ 連線逾時在正式環境很常見：網路中斷、防火牆擋掉、ES 整台當機都是。測試裡把 ES 停掉時，連線也是逾時，不是被拒絕。

## 應用要做什麼

用 app-common chart 的服務不用做任何事：chart 會在 Pod 加上三個 annotation，Collector 就會來抓（`otel.dotnet` 的 .NET 服務例外：不加 annotation，改用 OTLP 推，見 [dotnet.md](dotnet.md)）。

Java 的 metrics 也是 OTel：chart 設 `OTEL_METRICS_EXPORTER=prometheus`，OTel Java agent 在 9464 開 `/metrics`，由 OTel Collector 的 prometheus receiver 來抓；Prometheus 只是傳輸格式，沒有 Prometheus server。

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9464"
  prometheus.io/path: /metrics
```

沒用 chart 的服務，自己在 Pod template 加上這三個 annotation。Pod 的 CPU、記憶體、重啟次數不用做任何事，一定會收。

## 欄位

用 OTel 原生的格式存（ES 的 OTel mapping），欄位名稱是 OpenTelemetry 的標準名稱：

| 欄位 | 內容 | 對應 log 的欄位 |
|---|---|---|
| `metrics.<名稱>` | 數值，例如 `metrics.k8s.pod.cpu.usage`、`metrics.demo_requests_total` | — |
| `attributes.*` | 這個數值自己的維度，例如應用的 `path` | — |
| `resource.attributes.service.name` | Pod label `app.kubernetes.io/name`；沒有就用 Deployment 名稱 | `service.name` |
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

## 部署

### 1. 中央 ES

跟 log 共用同一份腳本，已經部署過 log 的話重跑一次，新發的 key 才有 metrics 的權限：

```bash
ES_URL=https://<es>:9200 ES_AUTH=elastic:<password> telemetry/scripts/elasticsearch-setup.sh
```

ES 要 9.x：要有內建的 `metrics-otel@template`，以及 OTLP 入口 `/_otlp/v1/metrics`（測試用 9.5.5）。上線前先確認：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -u elastic:<password> -X POST -H "Content-Type: application/x-protobuf" https://<es>:9200/_otlp/v1/metrics
```

回 `200` 才有；`404` 表示這個版本沒有，要先升級。

### 2. 每個應用 cluster

`otel-collector` namespace 要有 `es-writer-keys`（log 和 metrics 共用這一份）。怎麼建、在 Rancher 上要注意什麼，見 [log/README.md 的「部署」](../log/README.md#部署)。

改好 `telemetry/collectors/kustomization.yaml` 的 `CLUSTER_NAME`、`ES_ENDPOINT` 和 image 路徑，然後：

```bash
kubectl apply -k telemetry/collectors
```

## 驗證

```bash
ES_URL=http://<ip>:9200 e2e/local-k3s/run-metrics-e2e.sh
```

在單節點 k3s 上跑過（ES 9.5.5、OpenTelemetry Collector contrib 0.162.0），**37 項全部通過**：

| 類別 | 檢查 |
|---|---|
| 分流 | 三種來源各自：IT／IMC／unassigned 都只有該進來的 namespace，沒有落錯、沒有送兩份 |
| 分流的重點 | IT 的 Pod CPU（kubelet 抓的）進 IT；Deployment 副本數（沒有 Pod 的物件）進 IT；節點本身的資料進 unassigned |
| 欄位 | 服務名稱、版本、part-of、group、dept、cluster 都對；應用自稱的 `dept`、`group` 被拿掉；應用自己的維度（`path`）保留 |
| 存法 | 資料流是 `time_series`（TSDS）；可寫入時間往後 2 小時 |
| 權限 | 寫入 key 能寫所有 group 的 metrics、不能讀（2026-10 起一把共用，之前每個 group 一把）；AIOps key 能讀全部、不能寫；IT 的唯讀角色看得到 IT 的 metrics |

情境測試（`ES_URL=... e2e/local-k3s/run-metrics-scenarios.sh label|outage|keyfail`）：

| 情境 | 結果 |
|---|---|
| 改 namespace 的 dept label（不重啟 Collector） | **生效時間 2～17 秒**（跑了兩次），最多就是一個抓取週期（30 秒）；切換前後沒有漏、沒有重複 |
| ES 整台停 3 分鐘 | 停機期間的應用 metrics 各 6 筆、IT 的 Pod CPU 24 筆，**全部補齊**；ES 恢復後 5 秒內補完 |
| ES 整台停 10 分鐘，**中途重啟兩個 Collector** | 節點上的佇列堆到 4.8M。重啟前在佇列裡的、重啟後收到的，三個 group **全部補進 ES**（各 20 筆）；沒有重複；failure store 0 筆；Collector 丟資料的紀錄 0 筆 |
| IMC 的 key 作廢 3 分鐘（每個 group 一把 key 時的結果；現在一把共用，作廢就是全部停住，腳本已改成驗這個、還沒在 k3s 重跑） | IT 照常寫入（6 筆），只有 IMC 停住；換新 key、重啟 Collector 後 IMC 恢復。**失效那 3 分鐘的 IMC metrics 遺失（0 筆），補不回來**，見下方「坑」 |

每個 group 的佇列上限是 10,000 批（`queue_size`）。超過時新的資料會被丟掉，所以中央 ES 的停機時間要控制在佇列撐得住的範圍；量大的 cluster 要先估算或調大。

## 坑

集中在 [troubleshooting.md](../troubleshooting.md#metrics)。

## 還沒做

1. **保留天數**：已套用（[retention.md](../retention.md)），各 group 要不要不同還要確認。
2. **量大時夠不夠力**：只用測試的量驗過（單節點、十幾個 Pod）。佇列 10,000 批在量大時能撐多久停機也還沒量。
3. **節點本身的系統指標**（磁碟、檔案系統、load）：現在只有 kubelet 給的節點 CPU、記憶體、網路，沒有收 hostmetrics。
4. **K8s events**：跟 log 那邊一樣還沒做。

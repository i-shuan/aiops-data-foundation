# Metrics 進 Elasticsearch

一句話：**每個 cluster 一套 OpenTelemetry Collector 收 metrics，依「每筆資料自己的 namespace」讀 dept 分 group，各 group 用自己的 key 寫進 ES 的 time series data stream。**

log 也由同一個 Collector 收（[elasticsearch.md](elasticsearch.md)）：分部門的規則只有一份（`common.yaml` 的 `transform/group`，log 和 metrics 共用）、key 也共用，同一個 Pod 的 log 和 metrics 一定落在同一個 group。

## 資料怎麼走

```
每個節點一個 Collector（DaemonSet）
   ├─ kubelet：節點、Pod、容器的 CPU、記憶體、網路、磁碟
   └─ 應用的 Prometheus metrics：這個節點上、Pod 標註 prometheus.io/scrape: "true" 的
整個 cluster 一個 Collector（Deployment，replicas 1）
   └─ k8s_cluster：Pod 狀態、容器重啟次數、Deployment/ReplicaSet 副本數、namespace、節點狀態
        ↓ 兩種 Collector 共用同一份規則（common.yaml）
        ↓ 1. 擋冒充：應用自帶的 dept、group 欄位一律拿掉
        ↓ 2. 依這筆資料屬於哪個 Pod／物件，讀它 namespace 的 dept label，補上服務身分
        ↓ 3. dept → group（清單同 platform/groups.yaml）；沒有 dept 的（例如節點本身）→ unassigned
        ↓ 4. 依 group 交給各自的出口，每筆只送一份；用 OTLP 送到 ES 的 /_otlp/v1/metrics
   ├─ group it         → key: IT_ES_API_KEY         → metrics-<來源>.otel-it
   ├─ group imc        → key: IMC_ES_API_KEY        → metrics-<來源>.otel-imc
   └─ group unassigned → key: UNASSIGNED_ES_API_KEY → metrics-<來源>.otel-unassigned
        ↓ 中央 ES 9 內建的 metrics-otel@template 把它建成 time series data stream（TSDS）
   AIOps 用唯讀 key 讀 metrics-*.otel-*；人用各 group 的唯讀角色
```

`<來源>` 是 `kubeletstatsreceiver`、`k8sclusterreceiver`、`prometheusreceiver` 三種，所以每個 group 有三條資料流。

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
| [platform/cluster-addons/otel-collector/common.yaml](../platform/cluster-addons/otel-collector/common.yaml) | 兩種 Collector 共用：擋冒充、讀 dept、dept → group、每個 group 的出口 |
| [platform/cluster-addons/otel-collector/node.yaml](../platform/cluster-addons/otel-collector/node.yaml) | 節點那一份收什麼：kubelet、應用的 Prometheus metrics |
| [platform/cluster-addons/otel-collector/cluster.yaml](../platform/cluster-addons/otel-collector/cluster.yaml) | cluster 那一份收什麼：K8s 物件的狀態 |
| [platform/cluster-addons/otel-collector/](../platform/cluster-addons/otel-collector) 其餘 | 權限、DaemonSet、Deployment、kustomization |
| [platform/central/elasticsearch-setup.sh](../platform/central/elasticsearch-setup.sh) | 跟 log 共用：寫入 key、唯讀角色、AIOps key 都涵蓋 metrics |
| [platform/central/elasticsearch/metrics-otel-custom.json](../platform/central/elasticsearch/metrics-otel-custom.json) | TSDS 可寫入時間往後 2 小時（見「坑」：ES 重啟後拒收） |
| [test/local-k3s/run-metrics-e2e.sh](../test/local-k3s/run-metrics-e2e.sh) | 端到端測試 |
| [test/local-k3s/run-metrics-scenarios.sh](../test/local-k3s/run-metrics-scenarios.sh) | 情境測試：改 label、ES 停機加重啟 Collector、key 失效 |

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

用 app-common chart 的服務不用做任何事：chart 會在 Pod 加上三個 annotation，Collector 就會來抓。

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

### 應用的請求數、錯誤、延遲（AIOps baseline 用）

Java agent 會開出 `http.server.request.duration`（每個 HTTP 請求的耗時直方圖，依 method、route、狀態碼分），這是 AIOps baseline 算流量、錯誤率、延遲的來源（見 [aiops-rca.md「分工」](aiops-rca.md#分工metrics-決定什麼時候告警trace-和-log-說明原因)）。

| | |
|---|---|
| ES 欄位 | `metrics.http_server_request_duration_seconds`，型別 `exponential_histogram`（ES 9.5 自動轉） |
| 維度 | `attributes.http_request_method`、`attributes.http_route`、`attributes.http_response_status_code` |
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
ES_URL=https://<es>:9200 ES_AUTH=elastic:<password> platform/central/elasticsearch-setup.sh
```

ES 要 9.x：要有內建的 `metrics-otel@template`，以及 OTLP 入口 `/_otlp/v1/metrics`（測試用 9.5.5）。上線前先確認：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -u elastic:<password> -X POST -H "Content-Type: application/x-protobuf" https://<es>:9200/_otlp/v1/metrics
```

回 `200` 才有；`404` 表示這個版本沒有，要先升級。

### 2. 每個應用 cluster

`otel-collector` namespace 要有 `es-writer-keys`（log 和 metrics 共用這一份）。怎麼建、在 Rancher 上要注意什麼，見 [elasticsearch.md 的「部署」](elasticsearch.md#部署)。

改好 `platform/cluster-addons/otel-collector/kustomization.yaml` 的 `CLUSTER_NAME`、`ES_ENDPOINT` 和 image 路徑，然後：

```bash
kubectl apply -k platform/cluster-addons/otel-collector
```

## 驗證

```bash
ES_URL=http://<ip>:9200 test/local-k3s/run-metrics-e2e.sh
```

在單節點 k3s 上跑過（ES 9.5.5、OpenTelemetry Collector contrib 0.162.0），**37 項全部通過**：

| 類別 | 檢查 |
|---|---|
| 分流 | 三種來源各自：IT／IMC／unassigned 都只有該進來的 namespace，沒有落錯、沒有送兩份 |
| 分流的重點 | IT 的 Pod CPU（kubelet 抓的）進 IT；Deployment 副本數（沒有 Pod 的物件）進 IT；節點本身的資料進 unassigned |
| 欄位 | 服務名稱、版本、part-of、group、dept、cluster 都對；應用自稱的 `dept`、`group` 被拿掉；應用自己的維度（`path`）保留 |
| 存法 | 資料流是 `time_series`（TSDS）；可寫入時間往後 2 小時 |
| 權限 | IT 的 key 只能寫 IT 的 metrics、不能讀；AIOps key 能讀全部、不能寫；IT 的唯讀角色看得到 IT 的 metrics |

情境測試（`ES_URL=... test/local-k3s/run-metrics-scenarios.sh label|outage|keyfail`）：

| 情境 | 結果 |
|---|---|
| 改 namespace 的 dept label（不重啟 Collector） | **生效時間 2～17 秒**（跑了兩次），最多就是一個抓取週期（30 秒）；切換前後沒有漏、沒有重複 |
| ES 整台停 3 分鐘 | 停機期間的應用 metrics 各 6 筆、IT 的 Pod CPU 24 筆，**全部補齊**；ES 恢復後 5 秒內補完 |
| ES 整台停 10 分鐘，**中途重啟兩個 Collector** | 節點上的佇列堆到 4.8M。重啟前在佇列裡的、重啟後收到的，三個 group **全部補進 ES**（各 20 筆）；沒有重複；failure store 0 筆；Collector 丟資料的紀錄 0 筆 |
| IMC 的 key 作廢 3 分鐘 | IT 照常寫入（6 筆），只有 IMC 停住；換新 key、重啟 Collector 後 IMC 恢復。**失效那 3 分鐘的 IMC metrics 遺失（0 筆），補不回來**，見下方「坑」 |

每個 group 的佇列上限是 10,000 批（`queue_size`）。超過時新的資料會被丟掉，所以中央 ES 的停機時間要控制在佇列撐得住的範圍；量大的 cluster 要先估算或調大。

## 坑

- **key 失效期間的資料會遺失（metrics 和 log 都是）。** OTLP 出口只對 429、502、503、504 重試，401（key 錯、被作廢）當成永久錯誤，整批直接丟掉，也沒有設定可以改。所以：
  - **換 key 用 `platform/central/rotate-writer-key.sh`**：先發新的、換進 Secret、等每個 Collector 換用新 key，才作廢舊的；Collector 從檔案讀 key，不用重啟，metrics 也就沒有重啟造成的空檔。實測見 [elasticsearch.md 的「換 key」](elasticsearch.md#換-key)。
  - 現在發的 key 沒有到期日，不會自己失效。之後若要加到期日，要排好在到期前換。
  - 要監控 Collector 的 `otelcol_exporter_send_failed_metric_points`、`otelcol_exporter_send_failed_log_records`（`:8888/metrics`），大於 0 就是有資料送不出去被丟掉。
  - log 另外要看 Collector 自己的 log 裡的 `Partial success`：ES 拒收其中幾筆時只印這行、那幾筆丟掉。其中 `CONFLICT`（409）是重送的被擋掉，屬於正常的去重複，不算遺失。
- **ES 重啟後，TSDS 曾經拒收停機期間的資料，而且回成功。** TSDS 每個 backing index 只收一段時間範圍內的資料，往後預設只開 9 分鐘（`index.look_ahead_time`）。ES 停機期間時間範圍沒有往後推，重開時佇列裡的資料加上新資料一起湧入，落在範圍外的被 ES 丟進 failure store（`.fs-metrics-*`），**回給 Collector 的卻是成功**，Collector 不會報錯也不會重送。實測停 3 分鐘時有 4,162 筆進了 failure store。改法：`metrics-otel@custom` 把 `look_ahead_time` 設成 2 小時（`elasticsearch-setup.sh` 會建），之後停 10 分鐘 0 筆。設定只對新的 backing index 生效，既有的資料流要 rollover 一次。要監控 failure store：
  ```bash
  curl -s -u elastic:<password> "https://<es>:9200/.fs-metrics-*/_count?expand_wildcards=all&ignore_unavailable=true"
  ```
- **重啟 Collector 會少抓 1～2 次。** 舊的停掉到新的開始抓之間（約 30～60 秒）沒有人抓，這段的應用 metrics、kubelet 資料就沒有，事後也補不回來。佇列裡已經抓到、還沒送出的不受影響（實測重啟後全部送出）。換版、改設定盡量排在離峰。
- **cluster 那一份的 replicas 一定要 1。** 多一個，K8s 物件狀態的每筆資料就多一份。換版時也用 `Recreate`，避免新舊同時跑。
- **改 namespace 的 dept 幾秒內生效**（實測 2～17 秒），log 和 metrics 同時切換。
- **dept 清單要手動同步兩處。** `platform/groups.yaml` 和 `common.yaml` 的 `transform/group`（log 和 metrics 共用）要一致。
- **應用的 `service.name` 曾經全部是 `pods`。** Prometheus receiver 會把抓取工作的名稱填進 `service.name`，而讀 Pod label 的那一步不覆蓋已經有的值。`common.yaml` 先清掉再讀，測試裡有一項專門擋這個。
- **不要換回 `elasticsearch` 寫入元件。** 它在連線逾時時會直接丟資料，見上方「為什麼用 OTLP 送」。
- **向 kubelet 取資料時沒有驗它的憑證**（`insecure_skip_verify: true`）。正式環境若 kubelet 憑證由內部 CA 簽，改成驗證。
- **`CLUSTER_NAME` 是每筆資料的 `k8s.cluster.name`**，不在資料流名稱裡（跟 log 不一樣）。每個 cluster 一定要改，不然各廠的資料會混在一起分不出來。

## 還沒做

1. **保留天數**：現在用 ES 內建的預設，要跟各 group 確認後設定。
2. **量大時夠不夠力**：只用測試的量驗過（單節點、十幾個 Pod）。佇列 10,000 批在量大時能撐多久停機也還沒量。
3. **節點本身的系統指標**（磁碟、檔案系統、load）：現在只有 kubelet 給的節點 CPU、記憶體、網路，沒有收 hostmetrics。
4. **K8s events**：跟 log 那邊一樣還沒做。

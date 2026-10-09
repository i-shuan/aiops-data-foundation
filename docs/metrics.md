# Metrics 進 Elasticsearch

一句話：**每個 cluster 一套 OpenTelemetry Collector 收 metrics，依「每筆資料自己的 namespace」讀 dept 分 group，各 group 用自己的 key 寫進 ES 的 time series data stream。**

分部門的規則、key、`platform/groups.yaml` 都跟 log（[elasticsearch.md](elasticsearch.md)）共用，同一個 Pod 的 log 和 metrics 會落在同一個 group。

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
        ↓ 4. 依 group 交給各自的出口，每筆只送一份
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
| [platform/cluster-addons/otel-metrics/common.yaml](../platform/cluster-addons/otel-metrics/common.yaml) | 兩種 Collector 共用：擋冒充、讀 dept、dept → group、每個 group 的出口 |
| [platform/cluster-addons/otel-metrics/node.yaml](../platform/cluster-addons/otel-metrics/node.yaml) | 節點那一份收什麼：kubelet、應用的 Prometheus metrics |
| [platform/cluster-addons/otel-metrics/cluster.yaml](../platform/cluster-addons/otel-metrics/cluster.yaml) | cluster 那一份收什麼：K8s 物件的狀態 |
| [platform/cluster-addons/otel-metrics/](../platform/cluster-addons/otel-metrics) 其餘 | 權限、DaemonSet、Deployment、kustomization |
| [platform/central/elasticsearch-setup.sh](../platform/central/elasticsearch-setup.sh) | 跟 log 共用：寫入 key、唯讀角色、AIOps key 都涵蓋 metrics |
| [test/local-k3s/run-metrics-e2e.sh](../test/local-k3s/run-metrics-e2e.sh) | 端到端測試 |

兩種 Collector 啟動時都載入 `common.yaml` 加上自己那份，分部門的規則只有一處，不會兩邊各改各的。

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

## 部署

### 1. 中央 ES

跟 log 共用同一份腳本，已經部署過 log 的話重跑一次，新發的 key 才有 metrics 的權限：

```bash
ES_URL=https://<es>:9200 ES_AUTH=elastic:<password> platform/central/elasticsearch-setup.sh
```

ES 要 9.x（或 8.16 以上），才有內建的 `metrics-otel@template`。

### 2. 每個應用 cluster

`otel-metrics` namespace 也要一份 `es-writer-keys`，內容跟 `logging` 那份一樣。怎麼建、在 Rancher 上要注意什麼，見 [elasticsearch.md 的「部署」](elasticsearch.md#部署)。

改好 `platform/cluster-addons/otel-metrics/kustomization.yaml` 的 `CLUSTER_NAME`、`ES_ENDPOINT` 和 image 路徑，然後：

```bash
kubectl apply -k platform/cluster-addons/otel-metrics
```

## 驗證

```bash
ES_URL=http://<ip>:9200 test/local-k3s/run-metrics-e2e.sh
```

在單節點 k3s 上跑過（ES 9.5.5、OpenTelemetry Collector contrib 0.162.0），**36 項全部通過**：

| 類別 | 檢查 |
|---|---|
| 分流 | 三種來源各自：IT／IMC／unassigned 都只有該進來的 namespace，沒有落錯、沒有送兩份 |
| 分流的重點 | IT 的 Pod CPU（kubelet 抓的）進 IT；Deployment 副本數（沒有 Pod 的物件）進 IT；節點本身的資料進 unassigned |
| 欄位 | 服務名稱、版本、part-of、group、dept、cluster 都對；應用自稱的 `dept`、`group` 被拿掉；應用自己的維度（`path`）保留 |
| 存法 | 資料流是 `time_series`（TSDS） |
| 權限 | IT 的 key 只能寫 IT 的 metrics、不能讀；AIOps key 能讀全部、不能寫；IT 的唯讀角色看得到 IT 的 metrics |

情境測試：

| 情境 | 結果 |
|---|---|
| 改 namespace 的 dept label（不重啟 Collector） | **2 秒內生效**：02:02:49 改 label，02:02:51 那筆就進新的 group，切換前後沒有漏、沒有重複 |
| ES 整台停 3 分鐘 | 見下方 |

## 坑

- **cluster 那一份的 replicas 一定要 1。** 多一個，K8s 物件狀態的每筆資料就多一份。換版時也用 `Recreate`，避免新舊同時跑。
- **改 namespace 的 dept：metrics 2 秒生效，log 最多 15 分鐘。** Fluent Bit 會快取 namespace 的 label，這段時間同一個 namespace 的 log 和 metrics 會在不同 group。改 label 後重啟 Fluent Bit 就一致了：
  ```bash
  kubectl -n logging rollout restart daemonset/fluent-bit
  ```
- **dept 清單要手動同步。** `platform/groups.yaml`、Fluent Bit 的分流規則、`common.yaml` 的 `transform/group` 三處要一致。漏改一處的話，那個部門的 log 和 metrics 會分到不同 group，不會報錯。
- **應用的 `service.name` 曾經全部是 `pods`。** Prometheus receiver 會把抓取工作的名稱填進 `service.name`，而讀 Pod label 的那一步不覆蓋已經有的值。`common.yaml` 先清掉再讀，測試裡有一項專門擋這個。
- **向 kubelet 取資料時沒有驗它的憑證**（`insecure_skip_verify: true`）。正式環境若 kubelet 憑證由內部 CA 簽，改成驗證。
- **`CLUSTER_NAME` 是每筆資料的 `k8s.cluster.name`**，不在資料流名稱裡（跟 log 不一樣）。每個 cluster 一定要改，不然各廠的資料會混在一起分不出來。

## 還沒做

1. **保留天數**：現在用 ES 內建的預設，要跟各 group 確認後設定。
2. **量大時夠不夠力**：只用測試的量驗過（單節點、十幾個 Pod）。
3. **連線逾時**（不是被拒絕）時的行為：停機測試測的是 ES 停掉、連線被拒絕的情況，網路中斷造成的逾時還沒測。
4. **節點本身的系統指標**（磁碟、檔案系統、load）：現在只有 kubelet 給的節點 CPU、記憶體、網路，沒有收 hostmetrics。
5. **K8s events**：跟 log 那邊一樣還沒做。

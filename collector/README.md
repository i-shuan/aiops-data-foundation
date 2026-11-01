# collector：每個應用 cluster 一套 OpenTelemetry Collector（①②）

> **類型：操作 How-To**　原理見 [docs/log](../docs/log/README.md)、[docs/metrics](../docs/metrics/README.md)、[docs/trace](../docs/trace/README.md)

收 log、metrics、trace，在邊緣做身份鑑別與標籤注入、依 namespace 的 `dept` 分 group，寫進中央 ES。

| 資料夾 | 內容 |
|---|---|
| `code/` | Collector 設定：`common.yaml`（出口、分 group，三種資料共用）、`node.yaml`（DaemonSet：log 檔、kubelet、應用的 OTLP、Prometheus 抓取）、`cluster.yaml`（Deployment：K8s 物件狀態）；`groups.yaml`（部門 → group 對照，要跟 `common.yaml` 的 `transform/group`、ES setup 的角色一致） |
| `helmchart/` | 部署：node DaemonSet、cluster Deployment（1 份）、RBAC、Service。trace 100% 直接寫 ES、不取樣（合成一個請求一筆在 engine 做）。`values.yaml` 正式預設值、`values-test.yaml` 本機測試 |

## 部署

前提：中央 ES 已跑過 [elasticsearch-setup.sh](../elasticsearch/README.md)，拿到寫入 key（`ES_WRITER_API_KEY`）。

```bash
kubectl create namespace otel-collector
kubectl -n otel-collector create secret generic es-writer-keys --from-literal=ES_WRITER_API_KEY=<寫入 key>
helm upgrade --install collector collector/helmchart -n otel-collector \
  --set clusterName=<這個 cluster 的名字> --set esEndpoint=https://<中央 ES>:9200 --set ntpServer=<公司 NTP>:123 \
  --set image.repository=<nexus>/otel/opentelemetry-collector-contrib \
  --set-file config.common=collector/code/common.yaml,config.node=collector/code/node.yaml,config.cluster=collector/code/cluster.yaml
```

- 設定檔不複製進 chart，`--set-file` 從 `code/` 帶，`code/` 永遠是唯一的來源；改了設定再跑同一個指令，Pod 有 `checksum/config` 註記會自己滾動重啟。
- 每個 cluster **一定要改** `clusterName`（每筆資料的 `k8s.cluster.name`，兩個 cluster 一樣會混在一起）。
- 換寫入 key：[rotate-writer-key.sh](../elasticsearch/code/scripts/rotate-writer-key.sh)。Secret 改了 Collector 自己換用，不用重啟。
- 本機測試：`e2e/k8s/deploy-collector.sh`（用 `values-test.yaml`：ES 是同 cluster 的 es-test、trace 全留）。

## 公司已經有 Collector：AIOps 要加哪些

不用我們的 chart，也可以把 AIOps 需要的設定加到公司現有的 Collector。下面的區塊名稱都是 `code/` 裡的原文，**直接去 `code/` 複製**，這裡不再抄一份（會不同步）。我們用的是 `otel/opentelemetry-collector-contrib:0.162.0`；公司的版本比較舊，`extract_sum_metric`、`ntp` receiver 等可能沒有，先對版本。

### 必加：沒有它 AIOps 的判斷會錯或失效

| 區塊（檔案） | 做什麼 | AIOps 哪裡用 |
|---|---|---|
| `cumulativetodelta`（`include.metric_types: [histogram]`）、`transform/hist-sum`（`extract_sum_metric`）（`node.yaml`） | 直方圖從「應用啟動後一直累加」改成「這 30 秒的增量」，並另存 `<名稱>_sum`。順序：先 `cumulativetodelta`、再 `transform/hist-sum` | baseline 算每 5 分鐘的請求數、p95、錯誤率；規則 `jvm.gc_overhead` 讀 `_sum`。**沒有這個，p95 和請求數會算錯** |
| `metricstransform/java-http`、`transform/java-http`（`node.yaml`） | Java agent 的 Prometheus 名字（`http_server_request_duration_seconds`、`http_route`…）改成 OTel 名字，跟 .NET 一致；只對舊名字生效 | baseline、品質檢查、根因分析只寫一套。多一種語言（agent 名字不同）就在這裡多一組 |
| `service.name`／`service.version`／`service.namespace` 的設定：`k8s_attributes` 的 `labels`＋`transform/group` 前三行 `set(...)`、退回 Deployment 名稱那行（`common.yaml`） | 服務身分以 Pod 的 `app.kubernetes.io/name`、`/version`、`/part-of` 為準，蓋掉應用自己設的 `OTEL_SERVICE_NAME` | log、metrics、trace 用同一個服務名，才串得起來；Pod 同儕比對、baseline 依服務分組 |
| 寫入 ES 的出口 `elasticsearch/es`（`common.yaml`） | `mapping: {mode: otel}`、`timeout: 0`、`retry`（含 `retry_on_status: [401, 429, 502, 503, 504]`、`max_retries` 很大）、`sending_queue` 放磁碟（`file_storage`）、`batch` | ES 停機、剛重啟時（API key 一秒多被拒 401）資料留在佇列不丟。預設值會直接丟資料（實測丟過 1,840～16,640 行），見 `common.yaml` 的註解 |
| trace 100% 收、不取樣（`traces/in` pipeline） | 每一段一筆都送 | `trace-merge` 合成一個請求一筆，根因分析讀這份 |

### 選配：有對應的規則才需要

| 區塊（檔案） | 做什麼 | 對應的規則 |
|---|---|---|
| `ntp` receiver、`resource/node`、`metrics/ntp` pipeline（`node.yaml`；需要 `NTP_SERVER`） | 每台節點的時鐘跟公司 NTP 差多少，30 分鐘一次 | `node.clock_offset` |
| `prometheus/cadvisor`、`groupbyattrs/cadvisor`、`transform/cadvisor`、`metrics/cadvisor` pipeline（`node.yaml`） | 只抓 CPU 被限流（CFS throttling）的兩個數字 | `container.cpu_throttled` |
| `k8s_cluster` receiver（`cluster.yaml`，**整個 cluster 只能一個、`replicas` 一定是 1**） | Pod 狀態、重啟次數、Deployment 副本數、節點 Ready | `container.restarted`、`deployment.unavailable`、`pod.failed`、`node.not_ready` 等 |
| `transform/exceptions`（`node.yaml`） | 例外事件抄到 span 的 `exception.type`、`exception.message` | 例外吞噬檢測（有例外、但 status 不是 ERROR、回 200） |
| `kubeletstats`、`prometheus`（抓 `prometheus.io/scrape: "true"` 的 Pod）（`node.yaml`） | 節點、Pod、容器的 CPU／記憶體；應用的 metrics | Pod 同儕比對、`container.memory_near_limit`、`container.memory_leak` 等 |

### 歸屬與安全：沒有 AIOps 也會想做，但 `aiops-alerts` 的 `dept`、`group` 靠它

| 區塊（檔案） | 做什麼 |
|---|---|
| `transform/pre`、`transform/traces-pre`、`transform/metrics-otlp-pre`、`transform/logs-otlp-pre` | 擋冒充：應用自己帶的 `dept`、`group`、K8s 欄位、`data_stream.*` 一律不算，歸屬只看 namespace 的 dept label 和連線來源 IP |
| `k8s_attributes` 的 `dept`（從 namespace label）＋`transform/group`、`groups.yaml` | dept → group（`it`、`imc`、其他 `unassigned`），決定寫進哪個 data stream（`data_stream.namespace`）；dept 清單換成你們的，並跟 ES 的唯讀角色（`elasticsearch-setup.sh`）一致 |
| `transform/drop-log-attrs` | metrics、trace 拿掉只給 log 用的 pod label、container id，避免變成維度撐爆 time series |

### 順序與前提
- 每條 pipeline 的 processor 順序（`node.yaml` 的 `service.pipelines`）：`memory_limiter` → 擋冒充（`*-pre`）→ 名字統一（`java-http`）→ `cumulativetodelta` → `transform/hist-sum` → `k8s_attributes` → `transform/group` → `transform/drop-log-attrs`；OTLP 進來的 metrics 是先 `k8s_attributes`、`transform/group`、`transform/drop-log-attrs`，再 `cumulativetodelta`、`transform/hist-sum`。順序換了結果會不同，照 `code/` 抄。
- Pod 要有 `app.kubernetes.io/name`、`/version`、`/part-of` label（`apps/` 的 app-common chart 會統一蓋）；namespace 要有 `dept` label。
- ES 端要先跑過 `elasticsearch-setup.sh`（結構、保留天數、API key），Collector 才寫得進去（[elasticsearch/README.md](../elasticsearch/README.md)）。寫入用的 API key 放在 Collector 讀的檔（`/etc/es-keys/ES_WRITER_API_KEY`）。

## 驗證

`e2e/k8s/run-e2e.sh`（log）、`run-metrics-e2e.sh`、`run-trace-e2e.sh`（[docs/log](../docs/log/README.md)、[docs/metrics](../docs/metrics/README.md)、[docs/trace](../docs/trace/README.md) 各有結果）。Collector 自己的狀態在 `:8888/metrics`（佇列長度、送出失敗數）。

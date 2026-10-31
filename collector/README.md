# collector：每個應用 cluster 一套 OpenTelemetry Collector（①②）

> **類型：操作 How-To**　原理見 [docs/log](../docs/log/README.md)、[docs/metrics](../docs/metrics/README.md)、[docs/trace](../docs/trace/README.md)

收 log、metrics、trace，在邊緣做身份鑑別與標籤注入、依 namespace 的 `dept` 分 group，寫進中央 ES。

| 資料夾 | 內容 |
|---|---|
| `code/` | Collector 設定：`common.yaml`（出口、分 group，三種資料共用）、`node.yaml`（DaemonSet：log 檔、kubelet、應用的 OTLP、Prometheus 抓取）、`cluster.yaml`（Deployment：K8s 物件狀態）、`sampler.yaml`（trace-sampler：尾端取樣）；`groups.yaml`（部門 → group 對照，要跟 `common.yaml` 的 `transform/group`、ES setup 的角色一致） |
| `helmchart/` | 部署：node DaemonSet、cluster Deployment（1 份）、trace-sampler StatefulSet（2 台）、RBAC、Service。`values.yaml` 正式預設值、`values-test.yaml` 本機測試 |

## 部署

前提：中央 ES 已跑過 [elasticsearch-setup.sh](../elasticsearch/README.md)，拿到寫入 key（`ES_WRITER_API_KEY`）。

```bash
kubectl create namespace otel-collector
kubectl -n otel-collector create secret generic es-writer-keys --from-literal=ES_WRITER_API_KEY=<寫入 key>
helm upgrade --install collector collector/helmchart -n otel-collector \
  --set clusterName=<這個 cluster 的名字> --set esEndpoint=https://<中央 ES>:9200 --set ntpServer=<公司 NTP>:123 \
  --set image.repository=<nexus>/otel/opentelemetry-collector-contrib \
  --set-file config.common=collector/code/common.yaml,config.node=collector/code/node.yaml,config.cluster=collector/code/cluster.yaml,config.sampler=collector/code/sampler.yaml
```

- 設定檔不複製進 chart，`--set-file` 從 `code/` 帶，`code/` 永遠是唯一的來源；改了設定再跑同一個指令，Pod 有 `checksum/config` 註記會自己滾動重啟。
- 每個 cluster **一定要改** `clusterName`（每筆資料的 `k8s.cluster.name`，兩個 cluster 一樣會混在一起）。
- 換寫入 key：[rotate-writer-key.sh](../elasticsearch/code/scripts/rotate-writer-key.sh)。Secret 改了 Collector 自己換用，不用重啟。
- 本機測試：`e2e/k8s/deploy-collector.sh`（用 `values-test.yaml`：ES 是同 cluster 的 es-test、trace 全留）。

## 驗證

`e2e/k8s/run-e2e.sh`（log）、`run-metrics-e2e.sh`、`run-trace-e2e.sh`（[docs/log](../docs/log/README.md)、[docs/metrics](../docs/metrics/README.md)、[docs/trace](../docs/trace/README.md) 各有結果）。Collector 自己的狀態在 `:8888/metrics`（佇列長度、送出失敗數）。

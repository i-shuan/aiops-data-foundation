# Metrics 進 Elasticsearch：操作

> **類型：操作 How-To**　同主題：[原理](README.md)・[規格（欄位）](reference.md)・[指標目錄（規格）](catalog.md)・[metrics 能做什麼（原理）](capabilities.md)・[案例與實測](cases.md)・[.NET 原理](dotnet.md)・[.NET 操作](dotnet-how-to.md)・[.NET 規格](dotnet-reference.md)

應用要做什麼、怎麼部署、怎麼驗證、遇到問題怎麼辦。

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

## 部署

### 1. 中央 ES

跟 log 共用同一份腳本，已經部署過 log 的話重跑一次，新發的 key 才有 metrics 的權限：

```bash
ES_URL=https://<es>:9200 ES_AUTH=elastic:<password> telemetry/scripts/elasticsearch-setup.sh
```

ES 要 8.19 以上：要有內建的 `metrics-otel@template`（Collector 用 `elasticsearch` 寫入元件的 otel 模式寫入，不需要 ES 9 的 `/_otlp` 入口；原因見 [2026-10 相容性紀錄](../pdca/meta/2026-10-compat-es8-grafana13.md)）。上線前先確認：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -u elastic:<password> https://<es>:9200/_index_template/metrics-otel@template
```

回 `200` 才有；`404` 表示這個版本沒有，要先升級。

### 2. 每個應用 cluster

`otel-collector` namespace 要有 `es-writer-keys`（log 和 metrics 共用這一份）。怎麼建、在 Rancher 上要注意什麼，見 [log/how-to.md 的「部署」](../log/how-to.md#部署)。

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

佇列上限 2,000 萬筆（`common.yaml` 的 `queue_size`，所有 group 共用，log、metrics 各一份）。超過時新的資料會被丟掉，所以中央 ES 的停機時間要控制在佇列撐得住的範圍；量大的 cluster 要先估算或調大。ES 停 10、30 分鐘的 k3s 實測（log、metrics、trace 都掉 0）見 [durability](../pdca/phase1-collect/2026-10-durability.md)。

## 坑

集中在 [troubleshooting.md](../troubleshooting.md#metrics)。

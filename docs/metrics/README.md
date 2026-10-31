# Metrics 進 Elasticsearch

> **類型：原理 Explanation**　同主題：[規格（欄位）](reference.md)・[操作（部署、驗證）](how-to.md)・[指標目錄（規格）](catalog.md)・[metrics 能做什麼（原理）](capabilities.md)・[案例與實測](cases.md)・[.NET 原理](dotnet.md)・[.NET 操作](dotnet-how-to.md)・[.NET 規格](dotnet-reference.md)

一句話：**每個 cluster 一套 OpenTelemetry Collector 收 metrics，依「每筆資料自己的 namespace」讀 dept 分 group，各 group 用自己的 key 寫進 ES 的 time series data stream。**

log 也由同一個 Collector 收（[log/README.md](../log/README.md)）：分部門的規則只有一份（`common.yaml` 的 `transform/group`，log 和 metrics 共用）、key 也共用，同一個 Pod 的 log 和 metrics 一定落在同一個 group。

## 現況與待辦（①收值、②清資料、③存）

**已完成 ✅**

| 項目 | 狀態 |
|---|---|
| node Collector：kubelet、cAdvisor（CPU 限流）、應用的指標（Java 照標註來抓、.NET 用 OTLP 推）；cluster Collector：K8s 物件狀態 | ✅ 每 30 秒 |
| 依每筆資料自己的 namespace 分部門（寫進各部門的 data stream）、集中式資料寫入憑證 (Shared Ingest Token) 所有部門共用 | ✅ |
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
        ↓ 1. 邊緣身份鑑別（Edge Identity Sanitization）：應用自帶的 dept、group 欄位一律拿掉
        ↓ 2. 依這筆資料屬於哪個 Pod／物件，讀它 namespace 的 dept label，補上服務身分
        ↓ 3. dept → group（清單同 telemetry/groups.yaml）；沒有 dept 的（例如節點本身）→ unassigned
        ↓ 4. data_stream.namespace = group；一個出口、集中式資料寫入憑證（ES_WRITER_API_KEY），用 elasticsearch 寫入元件（otel 模式）寫進 ES
        ↓    → metrics-<來源>.otel-<group>（例如 metrics-prometheusreceiver.otel-it）
        ↓ 中央 ES（8.19 以上）內建的 metrics-otel@template 把它建成 time series data stream（TSDS）
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

## 為什麼用 elasticsearch 寫入元件，不用 OTLP 送

公司的 ES 是 8.19，沒有 ES 9 自己的 OTLP 入口（`/_otlp/v1/*`），所以 Collector 用 `elasticsearch` 寫入元件（`mapping: otel`）寫進 ES 內建的 `*-otel` data stream；寫出來的資料流名稱、欄位、分流跟 OTLP 入口一樣，中央 pipeline 照用（[2026-10 相容性紀錄](../pdca/meta/2026-10-compat-es8-grafana13.md)）。

這個元件預設的重試會掉資料，所以設定（`telemetry/collectors/common.yaml`）改成這樣（[實測](../pdca/meta/2026-10-collector-es-exporter.md)）：

| 預設的行為 | 改成 | 為什麼 |
|---|---|---|
| 重試只靠元件自己的 `retry`（`max_retries: 2`，幾百毫秒就放棄）；`retry_on_failure` 對它沒作用 | `retry` 一直重試（1 秒起、最多 30 秒一次） | ES 停機期間資料留在佇列，回來再送 |
| 401 當永久錯誤，整批丟 | `retry_on_status` 加 401（另有 429、502、503、504） | ES 剛起來的一秒多 API key 會被拒；這些狀態整個請求都沒寫入，重送不會重複 |
| `timeout` 是含重試的總期限，到期那批丟 | `timeout: 0` | 停機或連線掛住超過期限也不丟 |

結果：ES 沒回應 90 秒 0 遺失；ES 停機 180 秒只掉停機那一刻前後約 10 秒（3%）——ES 的 IP 消失、連線變成送出去沒人理時，這個元件不重試。正式環境 ES 前面要有 LB／VIP＋健康檢查，主機掛掉時回拒絕或 503，就不是這種情況。

## 還沒做

1. **保留天數**：已套用（[retention.md](../retention.md)），各 group 要不要不同還要確認。
2. **量大時夠不夠力**：只用測試的量驗過（單節點、十幾個 Pod）。佇列在量大時能撐多久停機也還沒量（測試的量：每秒 200 行 log，30 分鐘佔 285MB）。
3. **節點本身的系統指標**（磁碟、檔案系統、load）：現在只有 kubelet 給的節點 CPU、記憶體、網路，沒有收 hostmetrics。
4. **K8s events**：跟 log 那邊一樣還沒做。

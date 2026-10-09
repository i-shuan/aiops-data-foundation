# Trace 進 Elasticsearch

一句話：**應用把 trace 送給同一個節點上的 OpenTelemetry Collector，Collector 依「送來的連線是哪個 Pod」讀 namespace 的 dept 分 group，各 group 用自己的 key 寫進 ES。**

收 log、metrics 的也是同一個 Collector（[elasticsearch.md](elasticsearch.md)、[metrics.md](metrics.md)），分部門的規則和 key 共用。

## 資料怎麼走

```
應用（OpenTelemetry Java agent 等）
   ↓ OTLP 送到 Service otel-node-collector（internalTrafficPolicy: Local → 只連同一個節點的 Collector）
節點的 Collector
   ↓ 1. 擋冒充：應用自帶的 k8s.*、container.*、dept、group、data_stream.* 一律拿掉
   ↓ 2. 看連線的來源 IP 是哪個 Pod → 補上 namespace、Pod、Deployment、namespace 的 dept、服務版本
   ↓ 3. dept → group（跟 log、metrics 同一份規則）
   ├─ group it         → key: IT_ES_API_KEY         → traces-generic.otel-it
   ├─ group imc        → key: IMC_ES_API_KEY        → traces-generic.otel-imc
   └─ group unassigned → key: UNASSIGNED_ES_API_KEY → traces-generic.otel-unassigned
        ↓ 中央 ES：traces-otel pipeline 用 trace_id-span_id 當 _id（重送不會重複）
   AIOps 用唯讀 key 讀 traces-*.otel-*；人用各 group 的唯讀角色
```

**為什麼要「只連同一個節點」：** Collector 只記得自己節點上的 Pod（省記憶體），要靠連線的來源 IP 對到 Pod，所以應用一定要連到自己節點上那個。Service 的 `internalTrafficPolicy: Local` 保證這件事，也省掉跨節點的流量。

**為什麼要先拿掉應用自帶的欄位：** trace 是應用自己送的，資源欄位應用可以亂填（例如自稱是別的 namespace、別的部門）。拿掉之後，歸屬只看連線來源，應用無法冒充。

## 應用要做什麼

用 app-common chart 而且開了 Java agent（`otel.javaAgent.enabled: true`）的服務不用做任何事，chart 會設好：

| 環境變數 | 值 |
|---|---|
| `OTEL_TRACES_EXPORTER` | `otlp` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | `http://otel-node-collector.otel-collector.svc:4318/v1/traces` |
| `OTEL_TRACES_SAMPLER` | `parentbased_traceidratio`：上游決定要記的跟著記，自己開始的請求依比例 |
| `OTEL_TRACES_SAMPLER_ARG` | `otel.traces.sampleRatio`，預設 `1.0`（全部記）。請求量大的服務調低，例如 `0.1` |

沒用 chart 的服務，自己設上面這幾個環境變數。不是 Java 的服務，用該語言的 OpenTelemetry SDK，送到同一個位址。

## 跟 log、metrics 串起來

- **log ↔ trace：** Java agent 會把 `trace_id` 寫進 log（logback 的 MDC），中央 pipeline 存成 log 的 `trace.id`；trace 的欄位是 `trace_id`。實測兩邊對得到（見「驗證」）。AIOps 看到一筆錯誤 log，可以直接查那個請求的完整 trace。
- **trace ↔ metrics：** 都有 `resource.attributes.service.name`、`k8s.namespace.name`、`k8s.pod.name`。

## 欄位

用 OTel 原生的格式存（ES 9 內建的 `traces-otel@template`，logsdb 模式）：

| 欄位 | 內容 |
|---|---|
| `trace_id`、`span_id`、`parent_span_id` | 請求與這一段的 ID |
| `name`、`kind`、`duration`、`status.code` | 這一段做什麼、花多久、成功與否 |
| `attributes.*` | 這一段的細節，例如 `http.request.method`、`http.response.status_code` |
| `resource.attributes.service.name` | 應用自己設的（chart 設成 `app.kubernetes.io/name`） |
| `resource.attributes.service.version`、`.service.namespace` | Pod label，跟 log、metrics 一樣 |
| `resource.attributes.group`、`.dept` | 分流結果、namespace 的 dept |
| `resource.attributes.k8s.*` | namespace、Pod、節點、Deployment、cluster |

pod label、`container.*` 不會帶進 trace（只給 log 用）。

## 部署

中央 ES 和每個 cluster 的步驟跟 log、metrics 一樣（[elasticsearch.md 的「部署」](elasticsearch.md#部署)）：重跑 `elasticsearch-setup.sh`，新發的 key 才有 `traces-*.otel-*` 的權限（或用 `rotate-writer-key.sh` 一個一個 group 換，不掉資料）；`kubectl apply -k platform/cluster-addons/otel-collector` 會多一個 Service `otel-node-collector`。

ES 要有 `/_otlp/v1/traces`（測試用 9.5.5）：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -u elastic:<password> -X POST -H "Content-Type: application/x-protobuf" https://<es>:9200/_otlp/v1/traces
```

## 驗證

```bash
ES_URL=http://<ip>:9200 test/local-k3s/run-trace-e2e.sh
```

測試應用（[trace-app.sh](../test/local-k3s/trace-app.sh)）每 5 秒印一行帶 `trace_id` 的 log、送一個同 `trace_id` 的 span。it-mes-prod 那個故意在資源欄位自稱 imc-wip-prod、dept D1110、group imc、假的 Pod UID。**24 項全部通過**：

| 類別 | 檢查 |
|---|---|
| 分流 | 三個 namespace 的 trace 各進自己的 group，沒有落錯 |
| 擋冒充 | 自稱 IMC 的那個仍進 IT，namespace、dept、Pod UID 都是真的；IMC 裡沒有它的資料 |
| 欄位 | 服務名稱、版本、cluster、Pod 名稱；沒有夾帶 pod label |
| 串起來 | 從 trace 的 `trace_id` 找得到那行 log，反過來也找得到 |
| 去重複 | 同一個 span 送 3 次，ES 只有 1 筆（另外兩次回 409） |
| 權限 | IT 的 key 只能寫 IT 的 trace；AIOps key 能讀全部；IT 的唯讀角色看得到 IT 的 trace |

另外手動測過（每個測試應用每 5 秒一個 span）：

| 情境 | 結果 |
|---|---|
| ES 正常關機 3 分鐘 | 停機期間三個應用各 35～36 個 span 全部補進來，沒有重複 |
| ES `kill -9` 兩次（第 2 次在補送中） | 各 20 個 span 全部補進來，沒有重複 |

## 坑

- **Service 一定要 `internalTrafficPolicy: Local`。** 拿掉的話，應用可能連到別的節點的 Collector，那裡認不得這個 Pod，trace 會進 unassigned。
- **應用不能用 hostNetwork。** 用節點 IP 送出來的連線對不到 Pod，會進 unassigned。
- **key 失效期間的 trace 會遺失**，跟 log、metrics 一樣。換 key 用 `rotate-writer-key.sh`。
- **量**：trace 通常比 log 還大。請求量大的服務先調低 `otel.traces.sampleRatio`；全廠量起來前要估算 ES 的空間。

## 還沒做

1. **保留天數**：現在用 ES 內建的預設，要跟各 group 確認。
2. **取樣策略**：現在是各服務在源頭依比例取樣。要「錯誤和慢的請求一定留」的話，要改成在 Collector 收齊整個 trace 再決定（tail sampling），需要另外設計（同一個 trace 的 span 會分散在不同節點）。
3. **Kibana 的 APM 畫面**：資料是 OTel 原生格式，Kibana 的 Observability 應該能直接看，還沒確認。

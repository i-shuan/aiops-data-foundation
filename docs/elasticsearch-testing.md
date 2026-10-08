# Log 路線測試紀錄

驗證 [elasticsearch.md](elasticsearch.md) 的設計：Fluent Bit 收 log、依 dept 分流、中央 pipeline 整理欄位、API key 權限，以及某一把 key 失效時的行為。

## 測試環境

| 項目 | 內容 |
|---|---|
| K8s | k3s v1.32.2，單節點（4 核、15Gi RAM） |
| Elasticsearch | 8.17.2 單節點，開 security、HTTP 不走 TLS，heap 1g（[elasticsearch.yaml](../test/local-k3s/elasticsearch.yaml)） |
| Fluent Bit | 4.1.1，正式設定只換連線目標與 image 來源（[overlay](../test/local-k3s/fluent-bit/kustomization.yaml)） |
| API key | 用正式的 [elasticsearch-setup.sh](../platform/central/elasticsearch-setup.sh) 產生 |
| cluster 名稱 | `local`，所以 index 是 `k8s-{group}-local` |

這是模擬環境，連不到 Nexus、Rancher 和中央 ES。正式上線前要在實際的 cluster 再驗一次。

## 測資

三個 namespace 對應三種情境，定義在 [log-generators.yaml](../test/local-k3s/log-generators.yaml)。

| namespace | dept label | 模擬的服務 | 輸出格式 | 預期進哪 |
|---|---|---|---|---|
| `it-mes-prod` | `D3120` | `mes-api`（版本 2.3.1，part-of `mes`） | logback JSON | `k8s-it-local` |
| `imc-wip-prod` | `D1110` | `wip-worker`（版本 1.0.4，part-of `wip`） | ECS 巢狀 JSON | `k8s-imc-local` |
| `legacy` | 沒標 | 沒套 chart，只有 `app: legacy` label | 純文字 | `k8s-unassigned-local` |

各 namespace 的輸出內容如下。

**`it-mes-prod`**：每 2 秒一筆 INFO，每 5 筆夾一筆帶 stack trace 的 ERROR：

```json
{"timestamp":"2026-10-08T12:00:00.000Z","level":"INFO","message":"order 1 accepted","logger":"com.spil.mes.OrderService","thread":"http-nio-8080-exec-1","trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7","lot_id":"L1","@version":"1","level_value":20000}
{"timestamp":"2026-10-08T12:00:00.000Z","level":"ERROR","message":"order 5 failed","logger":"com.spil.mes.OrderService","thread":"http-nio-8080-exec-2","stack_trace":"java.lang.IllegalStateException: lot not found\n\tat com.spil.mes.OrderService.run(OrderService.java:42)","trace_id":"5bf92f3577b34da6a3ce929d0e0e4736"}
```

- `lot_id` 用來測「應用自帶欄位」會不會收進 `app`。
- `@version`、`level_value` 是 logstash encoder 會多輸出的雜訊欄位，用來測會不會被清掉。

**`imc-wip-prod`**：每 3 秒一筆，格式和 Spring Boot 3.4 的 `logging.structured.format.console=ecs` 相同：

```json
{"@timestamp":"2026-10-08T12:00:00.000Z","log":{"level":"WARN","logger":"wip.Queue"},"process":{"pid":1,"thread":{"name":"main"}},"message":"queue depth high","ecs":{"version":"8.11"}}
```

**`legacy`**：每 3 秒一行純文字：

```
plain text Thu Oct  8 12:00:00 UTC 2026
```

**順帶測到的第四種格式：** ES 自己的 log 也會被收進 unassigned。它是 ECS 的點號寫法，key 直接寫成 `"log.level"`、`"log.logger"`。

## 自動測試：33 項

跑法：

```bash
test/local-k3s/run-e2e.sh
# 已經連得到測試 ES 時（例如直接打 ClusterIP）：
ES_URL=http://<ip>:9200 test/local-k3s/run-e2e.sh
```

腳本會依序部署 ES、執行 setup 腳本拿到 key、建 Secret、部署 Fluent Bit 和三個測試服務，等 60 秒後開始檢查。

結果：**33 項全部通過。**

### 分流（7 項）

| 檢查 | 結果 |
|---|---|
| `it-mes-prod` 進 `k8s-it-local` | PASS（67 筆） |
| `imc-wip-prod` 進 `k8s-imc-local` | PASS（49 筆） |
| `legacy`（沒標 dept）進 `k8s-unassigned-local` | PASS（38 筆） |
| `k8s-it-local` 裡沒有別的 namespace | PASS |
| `k8s-imc-local` 裡沒有別的 namespace | PASS |
| it、imc 的資料沒有落到 unassigned | PASS |
| 每筆只送一份（三個 index 合計 = 全部） | PASS |

### 欄位（21 項）

一筆 logback 格式的 ERROR 經過 pipeline 後長這樣。這是用 `_ingest/pipeline/k8s-logs/_simulate` 跑的範例，所以 cluster 名稱和時間跟測試服務的實際輸出不同：

```json
{
  "@timestamp": "2026-10-08T12:00:00.123Z",
  "message": "order failed",
  "log": {"level": "error", "logger": "com.spil.mes.OrderService"},
  "process": {"thread": {"name": "http-nio-8080-exec-1"}},
  "error": {
    "type": "java.lang.IllegalStateException",
    "stack_trace": "java.lang.IllegalStateException: lot not found\n\tat ..."
  },
  "trace": {"id": "4bf92f3577b34da6a3ce929d0e0e4736"},
  "span": {"id": "00f067aa0ba902b7"},
  "service": {"name": "mes-api", "version": "2.3.1", "namespace": "mes"},
  "labels": {"group": "it", "dept": "D3120"},
  "orchestrator": {"cluster": {"name": "fab1"}},
  "kubernetes": {"namespace": "it-mes-prod", "pod": {"name": "mes-api-1"}, "...": "..."},
  "app": {"lot_id": "L123"},
  "event": {"ingested": "2026-10-08T12:03:08.897Z"}
}
```

| 檢查 | 結果 |
|---|---|
| `service.name` 來自 pod label | PASS |
| `service.version` | PASS |
| `service.namespace` 來自 part-of | PASS |
| `labels.group` | PASS |
| `labels.dept` 來自 namespace label | PASS |
| `orchestrator.cluster.name` | PASS |
| `log.level` 轉小寫 | PASS |
| `error.type` 從 stack trace 拆出 | PASS |
| `trace.id` | PASS |
| `@timestamp` 用應用自己的時間，不是 Fluent Bit 收到的時間 | PASS |
| 自訂欄位 `lot_id` 收進 `app` | PASS |
| logstash encoder 的雜訊欄位已移除 | PASS |
| 純文字 log 保留原文 | PASS |
| 沒套 chart 的服務，`service.name` 退回 container 名稱 | PASS |
| ECS 巢狀格式：`log.level` | PASS |
| ECS 巢狀格式：`log.logger` | PASS |
| ECS 巢狀格式：thread | PASS |
| ECS 巢狀格式：拿完的欄位不留空物件 | PASS |
| ECS 點號格式（ES 自己的 log）：`log.logger` | PASS |
| Fluent Bit 不收自己的 log | PASS |
| 沒有 pipeline 失敗的文件 | PASS |

### 權限（5 項）

| 檢查 | 預期 HTTP 狀態碼 | 結果 |
|---|---|---|
| IT 的 key 寫 IMC 的 index | 403 | PASS |
| IT 的 key 寫自己的 index | 201 | PASS |
| IT 的 key 讀資料 | 403 | PASS |
| AIOps 唯讀 key 讀全部 `k8s-*` | 200 | PASS |
| AIOps 唯讀 key 寫資料 | 403 | PASS |

## 故障測試：一把 key 失效（手動）

要驗證的設計說法有兩個：

1. 每個 group 的出口互相獨立，一把 key 失效只卡住那一條。
2. 卡住期間資料先存在節點硬碟，恢復後補送，不會遺失。

這段沒有放進自動測試，因為要等好幾分鐘，而且會換掉 key。

### 步驟

```bash
ES=http://<ip>:9200
AUTH=elastic:changeme-test-only

# 1. 作廢 IMC 的寫入 key，模擬 token 過期或被誤刪
curl -u $AUTH -X DELETE $ES/_security/api_key -H 'Content-Type: application/json' \
  -d '{"name":"fluent-bit-imc"}'

# 2. 等 45 秒，前後各數一次兩個 index 的筆數
curl -u $AUTH $ES/k8s-it-local/_count
curl -u $AUTH $ES/k8s-imc-local/_count

# 3. 發一把新的 IMC key，換進 Secret，重啟 Fluent Bit
#    （發 key 的內容與 elasticsearch-setup.sh 的 writer_key 相同）
kubectl -n logging rollout restart daemonset/fluent-bit

# 4. 等 40 秒，數失效那段時間的 IMC 資料有沒有補進來
curl -u $AUTH $ES/k8s-imc-local/_count -H 'Content-Type: application/json' \
  -d '{"query":{"range":{"@timestamp":{"gte":"<失效開始>","lte":"<失效結束>"}}}}'
```

### 結果

| 階段 | IT（`k8s-it-local`） | IMC（`k8s-imc-local`） |
|---|---|---|
| key 作廢前 | 78 筆 | 54 筆 |
| 作廢 45 秒後 | 106 筆，**照常寫入** | 54 筆，**停住** |
| 換新 key、重啟後 | 照常 | 失效那 45 秒的資料補進 **15 筆** |

IMC 測試服務每 3 秒輸出一筆，45 秒應該是 15 筆，**一筆都沒掉**。

而且這些資料是在 Fluent Bit Pod 重啟之後才補送的，代表它們確實存在節點硬碟（`/var/fluent-bit/state`），不是只放在記憶體裡。

### 結論

- 兩個設計說法都成立。
- 換 key 之後**一定要重啟 Fluent Bit**，它不會自己重讀 Secret。
- 堆積上限是每條出口 2G。key 失效太久、超過上限時會丟掉最舊的資料，所以要監控 Fluent Bit 的 metrics（`:2020/api/v2/metrics/prometheus`），看每條出口的重試次數。

## 測試中抓到的問題（已修）

### 1. Fluent Bit 收自己的 log，形成放大迴圈

**現象：** 第一輪測試時 key 沒拿到，三條出口全部被 ES 拒收（401）。之後 `logging` namespace 裡出現 17,824 筆 log，比三個測試服務加起來還多一百倍。

**原因：**
- 拒收時 Fluent Bit 會把錯誤印到 stdout；`trace_error: on` 的時候，連整批被拒的資料都會印出來。
- Fluent Bit 自己的 stdout 又被自己收進來，再送一次；如果又被拒，就再印一次。

正式環境的 ES 出狀況或 key 過期時，一樣會發生。

**修正：**
- `exclude_path` 排除 Fluent Bit 自己的 log。
- `trace_error` 預設關閉，只在排查問題時打開。
- 自動測試新增一項「Fluent Bit 不收自己的 log」。

### 2. ECS 格式的欄位沒有拆出來

**現象：** ES 自己的 log 進來後，只有 `log.level` 正確，`log.logger`、`process.thread.name` 都被留在 `app` 裡。

**原因：** pipeline 只認 logback 的 key，例如 `logger`、`thread`。ECS 用的是 `log.logger`、`process.thread.name`，而且有兩種寫法：點號寫在 key 裡，或巢狀物件。

**修正：**
- pipeline 改成兩種寫法都認得，拿完後清掉留下的空物件。
- 測試服務 `wip-worker` 改輸出 ECS 巢狀格式，並新增 5 項檢查。

### 3. 測試環境本身的問題（跟設計無關）

- **測試 ES 起來後是 red，發 key 卡住。** 測試機和別人共用磁碟，使用率 92%，超過 ES 預設 90% 的水位線，所以 shard 配置不出去。測試用的 ES 已關掉磁碟水位檢查；正式環境不要這樣做。
- **`elasticsearch-setup.sh` 遇到 ES 沒回應時會一直等。** 已加上 60 秒逾時，ES 有問題會直接失敗，而不是卡住。

## 還沒測的

- 真實環境：你們的 Fluent Bit 版本、中央 ES 的 TLS 憑證、Rancher 上的部署流程。
- namespace 的 dept label 改了之後多久生效。依 Fluent Bit 文件，namespace label 快取 15 分鐘，但沒有實測。
- 大流量：測資量很小，沒有測每秒上千筆時 Fluent Bit 和 ES pipeline 的負載。
- ES 整台停機，而不是單一 key 失效。理論上行為相同，都是堆在節點硬碟再補送，但沒有實測。

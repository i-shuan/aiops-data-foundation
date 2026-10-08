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

實際的原始 log 行和進 ES 後的文件，見[附錄 A](#a-三個常駐測試服務自動測試label-情境用)。

三個 namespace 對應三種情境，定義在 [log-generators.yaml](../test/local-k3s/log-generators.yaml)。另外兩個服務用來測「依格式分派」。

| namespace | dept label | 模擬的服務 | 輸出格式 | 預期進哪 |
|---|---|---|---|---|
| `it-mes-prod` | `D3120` | `mes-api`（版本 2.3.1，part-of `mes`） | logback JSON | `k8s-it-local` |
| `imc-wip-prod` | `D1110` | `wip-worker`（版本 1.0.4，part-of `wip`） | ECS 巢狀 JSON | `k8s-imc-local` |
| `legacy` | 沒標 | 沒套 chart，只有 `app: legacy` label | 純文字 | `k8s-unassigned-local` |
| `it-mes-prod` | `D3120` | `hold-svc`，宣告 `log-format: logfmt` | logfmt | `k8s-it-local`，走 logfmt 規則 |
| `it-mes-prod` | `D3120` | `eap`，宣告 `log-format: secs`（中央還沒有 secs 規則） | 一行仿 SECS 的純文字，**不是真實的 SECS 格式** | `k8s-it-local`，退回 default |

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

**`hold-svc`**：每 3 秒一行 logfmt：

```
ts=2026-10-08T12:00:00.000Z level=warn msg="lot hold requested" lot_id=L1 caller=hold.go:42
```

**`eap`**：每 3 秒一行。只是用來測「宣告了格式、但中央還沒有規則」的情況，內容不代表真實的 SECS log：

```
2026-10-08 12:00:00 [EQP01] RECV S6F11 W SystemBytes=1234
```

**順帶測到的第四種格式：** ES 自己的 log 也會被收進 unassigned。它是 ECS 的點號寫法，key 直接寫成 `"log.level"`、`"log.logger"`。

## 自動測試：43 項

跑法：

```bash
test/local-k3s/run-e2e.sh
# 已經連得到測試 ES 時（例如直接打 ClusterIP）：
ES_URL=http://<ip>:9200 test/local-k3s/run-e2e.sh
```

腳本會依序部署 ES、執行 setup 腳本拿到 key、建 Secret、部署 Fluent Bit 和三個測試服務，等 60 秒後開始檢查。

結果：**43 項全部通過。**

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

依格式分派加入後，下面這筆會多兩個欄位：`log.format`、`log.parser`，都是 `default`。

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

### 依格式分派（10 項）

實際的原始 log 行和進 ES 後的文件，見[附錄 E](#e-依格式分派)。

| 檢查 | 結果 |
|---|---|
| logfmt：走 logfmt 規則（`log.parser`） | PASS |
| logfmt：`log.level` | PASS |
| logfmt：`message` 取出 `msg` | PASS |
| logfmt：自訂欄位 `lot_id` 收進 `app` | PASS |
| logfmt：`@timestamp` 用 `ts` | PASS |
| 還沒有規則的格式：記下宣告的格式（`log.format` = secs） | PASS |
| 還沒有規則的格式：退回 default（`log.parser` = default） | PASS |
| 還沒有規則的格式：原文完整保留 | PASS |
| 沒宣告格式：`log.format` 是 default | PASS |
| 每筆都有 `log.parser` | PASS |

另外用 pipeline 模擬（`_simulate`）測過：宣告不合法的格式名稱（例如 `../Bad Format!`）會被當成 default，不會拿去組 pipeline 名稱。

### 權限（5 項）

| 檢查 | 預期 HTTP 狀態碼 | 結果 |
|---|---|---|
| IT 的 key 寫 IMC 的 index | 403 | PASS |
| IT 的 key 寫自己的 index | 201 | PASS |
| IT 的 key 讀資料 | 403 | PASS |
| AIOps 唯讀 key 讀全部 `k8s-*` | 200 | PASS |
| AIOps 唯讀 key 寫資料 | 403 | PASS |

## 故障測試：一把 key 失效（手動）

ES 實際回的錯誤，見[附錄 F](#f-key-失效權限情境)。

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

## 情境測試：dept label 改了之後多久生效

### 測資

改 label 前後的實際文件，見[附錄 B](#b-dept-label-情境)。

| 項目 | 內容 |
|---|---|
| 對象 | `legacy` namespace，原本沒標 dept，log 進 `k8s-unassigned-local` |
| 操作 | `kubectl label namespace legacy dept=D3120`，改完不重啟 Fluent Bit |
| log | `legacy` 服務照常每 3 秒一行純文字 |
| 怎麼判斷生效 | 用 log 本身的時間（`@timestamp`）找：最後一筆進 unassigned 和第一筆進 `k8s-it-local` 是什麼時候。用 log 時間而不是查到的時間，ES 慢或其他測試同時在跑都不影響結果 |
| 腳本 | 每 10 秒查一次，最多等 25 分鐘 |

### 結果

| 時間點 | 時刻 |
|---|---|
| Fluent Bit 啟動（`legacy` 的 namespace 資料從這時開始快取） | 12:32:37 |
| 改 label | 12:33:54 |
| 最後一筆進 unassigned | 12:47:36 |
| 第一筆進 IT | 12:47:39 |

- 改完 label 之後 **13 分 45 秒**才生效。
- 生效時刻是「Fluent Bit 第一次快取這個 namespace」後的 **15 分 2 秒**，剛好是 `kube_meta_namespace_cache_ttl` 的預設 15 分鐘，跟什麼時候改 label 無關。所以**最壞情況是改完要等滿 15 分鐘**。
- 切換很乾淨：前後兩筆只差 3 秒（一個 log 間隔），沒有漏掉也沒有重複。

### 結論

- 新 namespace 上線時，**先標 dept 再部署應用**，就不會有任何一筆進錯地方。Fluent Bit 第一次看到這個 namespace 時就會讀到正確的 label。
- 已經在跑的 namespace 補標或改標，那 15 分鐘內的資料會留在舊的 group。要立刻生效，就重啟 Fluent Bit：
  ```bash
  kubectl -n logging rollout restart daemonset/fluent-bit
  ```
- 也可以把 `kube_meta_namespace_cache_ttl` 調短，但每個節點都會更常去問 API server。目前維持預設。

## 情境測試：大流量

### 測資

實際的原始 log 行和進 ES 後的文件，見[附錄 C](#c-大流量es-停機情境同一個產生器)。

產生器定義在 [load/generator.yaml](../test/local-k3s/load/generator.yaml)，跑法見 [load/run-load.sh](../test/local-k3s/load/run-load.sh)。

| 項目 | 內容 |
|---|---|
| namespace | `load-test`，dept=`D1110`，所以進 `k8s-imc-local` |
| 產生器 | Python，依指定速率輸出，每段 120 秒 |
| 速率 | 每秒 500、2,000、5,000 行 |
| 每行內容 | logback 格式 JSON，約 420 bytes，接近一般 Java 服務的 INFO log |
| 怎麼算遺失 | 每行帶 `run`（這一段的編號）和 `seq`（從 1 開始的序號）。產生器結束時回報實際送出幾行，直接讀節點上的檔案，不經過 Fluent Bit。ES 裡數該 run 有幾個不同的 seq，就知道漏了哪幾號 |
| 怎麼算重複 | 同一個 seq 出現不只一次的多餘筆數 |
| 延遲 | `event.ingested`（ES 寫入時間）減 `@timestamp`（產生器輸出時間） |
| 資源 | 每 5 秒讀一次節點上 `/proc` 裡 Fluent Bit 和 ES 的 CPU、記憶體 |

範例一行：

```json
{"timestamp":"2026-10-08T12:44:00.123Z","level":"INFO","message":"load xxxx…(250 個 x)","logger":"load.Gen","thread":"main","run":"r5000-124225","seq":439390}
```

每秒 5,000 行時，產生器實際只輸出到約每秒 4,800 行，因為和 ES、Fluent Bit 擠同一台 4 核的機器。

### 第一輪結果（修正前）

| 速率 | 送出 | 收到 | 延遲 p50 / p99 | 最大延遲 | Fluent Bit CPU / 記憶體 | ES CPU / 記憶體 | pipeline 每筆 |
|---|---|---|---|---|---|---|---|
| 500 | 59,645 | 59,645 | 0.6s / 9.2s | 9.9s | 5% / 45MB | 14% / 1.6GB | 0.015ms |
| 2,000 | 237,740 | 237,740 | 0.7s / 9.4s | 9.9s | 10% / 70MB | 22% / 1.6GB | 0.011ms |
| 5,000 | 577,550 | 582,514 | 0.6s / 9.6s | 24.5s | 18% / 69MB | 48% / 1.6GB | 0.012ms |

- 每秒 500、2,000 行完全正確。
- 每秒 5,000 行時**收到比送出還多**，抓到一個重複寫入的 bug，見下方「問題 3」。
- 修正後再跑一次 5,000 行，換成**開頭 24,100 行全部遺失**，又抓到一個 bug，見下方「問題 4」。
- p99 延遲約 9 秒、最大約 10 秒，是因為 Fluent Bit 每 10 秒才掃一次新檔案，新 Pod 開頭的幾行要等它發現。問題 4 修好後這個延遲也會跟著下降。

### 最終結果（兩個問題都修正後）

| 速率 | 送出 | 收到 | 遺失 | 重複 | 延遲 p50 / p99 | 最大延遲 | Fluent Bit CPU / 記憶體 | ES CPU / 記憶體 | pipeline 每筆 |
|---|---|---|---|---|---|---|---|---|---|
| 500 | 59,520 | 59,520 | 0 | 0 | 0.5s / 1.4s | 2.5s | 6% / 30MB | 6% / 1.6GB | 0.010ms |
| 2,000 | 238,160 | 238,160 | 0 | 0 | 0.5s / 2.2s | 2.7s | 11% / 48MB | 15% / 1.6GB | 0.010ms |
| 5,000 | 584,200 | 584,200 | 0 | 0 | 0.5s / 2.3s | 2.6s | 19% / 48MB | 44% / 1.7GB | 0.012ms |

這段期間 IMC 出口共送出 882,032 筆（三段測試加上 wip-worker 的 log），Fluent Bit 的重試、丟棄都是 0。

CPU 的 100% 等於一個核心。

- **三段都沒有遺失、沒有重複。**
- **延遲：** p99 從約 9 秒降到約 2 秒，因為掃新檔案的間隔從 10 秒改成 2 秒。
- **Fluent Bit 很輕：** 每秒近 5,000 行只用約 0.2 核、不到 50MB 記憶體，`limits.memory: 512Mi` 綽綽有餘。
- **pipeline 不是瓶頸：** 每筆約 0.01ms，每秒 5,000 筆只用掉約 0.06 核。ES 的 CPU 主要花在寫入和建索引。
- **規劃參考：** 單一 ES 節點（1GB heap）在每秒 5,000 筆時用掉約 0.44 核。實際容量要看中央 ES 的規模，不能直接套用。

## 情境測試：ES 整台停機

### 測資

產生器和大流量測試相同，實際樣本見[附錄 C](#c-大流量es-停機情境同一個產生器)。

跑法見 [load/run-outage.sh](../test/local-k3s/load/run-outage.sh)。

| 項目 | 內容 |
|---|---|
| 產生器 | 同大流量測試，每秒 100 行，共 7 分鐘，每行帶序號 |
| 時間軸 | 開始 60 秒後把 ES 關掉（`kubectl scale --replicas=0`），關 3 分鐘再開回來，之後再跑 2 分鐘 |
| 測試 ES | 掛了硬碟（PVC），重啟後資料還在，比較接近正式環境 |
| 要回答的問題 | 1. 停機那 3 分鐘產生的約 18,000 行有沒有補齊；2. ES 回來後多久補完；3. 停機前已經寫進 ES 的資料還在不在；4. 停機期間 Fluent Bit 在節點上堆了多少；5. 有沒有重複 |

### 第一輪結果（重試間隔用預設值）

| 時刻 | 事件 |
|---|---|
| 12:58:55 | 產生器開始 |
| 12:59:55 | 關掉 ES，此時 ES 裡有 2,357,254 筆 |
| 12:59:55 ~ 13:02:58 | ES 停機。Fluent Bit 重試 632 次，節點上堆積 25MB |
| 13:03:35 | ES 重新可用 |
| 13:08:50 | 最後一筆補進來 |

| 問題 | 結果 |
|---|---|
| 停機期間的資料有沒有補齊 | **有**。全程送出 41,619 筆，最後收到 41,619 筆，**0 遺失** |
| 有沒有重複 | **0 重複** |
| 停機前的資料還在不在 | **在**。關機時 2,357,254 筆，重啟後一筆不少 |
| ES 恢復後多久補完 | **5 分 15 秒**，太慢，見下方「問題 5」 |

**中間一度誤判成遺失 500 筆：**
- 腳本原本看到筆數 30 秒沒變，就當作補完了。當時少的是停機期間 5 段、各剛好 100 筆（產生器 1 秒的量）。
- 其實那 5 段還放在節點硬碟上，排隊等下一次重試。
- 腳本已經改成等到筆數到齊為止。

### 第二輪結果（重試間隔上限改成 60 秒）

| 時刻 | 事件 |
|---|---|
| 13:09:05 | 產生器開始 |
| 13:10:06 | 關掉 ES，此時 ES 裡有 2,399,819 筆 |
| 13:10:06 ~ 13:13:09 | ES 停機。Fluent Bit 重試 667 次，節點上堆積 25MB |
| 13:13:46 | ES 重新可用 |
| 13:14:48 | 停機期間的資料全部補完 |

| 問題 | 第一輪 | 第二輪 |
|---|---|---|
| 遺失 | 0 / 41,619 | 0 / 41,567 |
| 重複 | 0 | 0 |
| 停機前的資料 | 全部還在 | 全部還在 |
| **ES 恢復後補完停機期間的資料** | **5 分 15 秒** | **62 秒** |

### 結論

- **ES 整台停機不會掉資料**：停機期間的資料先存在節點硬碟，恢復後自動補送，也不會重複。
- **堆積速度：** 每秒 100 行停 3 分鐘，堆了 25MB。照比例，每條出口 2G 的上限可以撐：
  - 每秒 100 行約 4 小時；
  - 每秒 1,000 行約 25 分鐘。
  - 超過就從最舊的開始丟。中央 ES 的維護時間要控制在這之內，或把 `storage.total_limit_size` 調大（節點硬碟要夠）。
- **恢復速度**由重試間隔上限決定。改成 60 秒後，ES 恢復約 1 分鐘內就補完。

## 情境測試：寫入去重複

### 做法

Fluent Bit 的 es output 加上 `generate_id: on`：

- 每筆用整筆記錄的內容算出一個 hash，當成 `_id`。記錄裡包含 container runtime 給的奈秒時間、pod、container 和 log 內容。
- 重送時送的是一模一樣的資料，`_id` 也一樣。ES 用 `create` 寫入時發現 `_id` 已存在，就回 409，不會再寫一份。
- Fluent Bit 把 409 當成成功，不會再重試。

### 測資

| 測試 | 內容 | 要確認什麼 |
|---|---|---|
| 強迫重送 | 測試專用設定，故意把 `buffer_size` 改回 512KB，重現問題 3 的「ES 已寫入、Fluent Bit 以為失敗而重送」，同時打開 `generate_id`；每秒 5,000 行跑 2 分鐘 | 重送的資料有沒有被擋掉；重試會不會停下來 |
| 單獨量成本 | 正式設定加上 `generate_id`，不製造重送，每秒 5,000 行跑 2 分鐘 | 去重複本身讓 ES 多花多少 |
| 相同內容 | 一個 Pod 用 `yes` 一口氣輸出 10,000 行完全相同的 JSON（`{"level":"INFO","message":"heartbeat ok",...}`） | 內容一樣的兩行會不會被誤判成重複而被丟掉 |

強迫重送的跑法見 [load/run-dedup.sh](../test/local-k3s/load/run-dedup.sh)。實際樣本見[附錄 D](#d-去重複情境)。

### 結果

| 每秒 5,000 行 | 重送次數 | 遺失 | 重複 | 延遲 p50 / p99 | ES CPU |
|---|---|---|---|---|---|
| 沒開 `generate_id`，有重送（問題 3 修正前） | 持續增加，停不下來 | 0 | 4,964 以上，還在增加 | 0.6s / 9.6s | 48% |
| **開 `generate_id`，強迫重送** | **30 次，之後停止** | **0** | **0** | 0.6s / 2.1s | 74% |
| 沒開 `generate_id`，沒有重送（基準） | 0 | 0 | 0 | 0.5s / 2.3s | 44% |
| **開 `generate_id`，沒有重送** | 0 | 0 | 0 | 0.5s / 1.0s | **53%** |

相同內容測試：10,000 行完全相同的內容，**ES 收到 10,000 筆**，沒有誤判。節點上的原始 log 裡，這 10,000 行的奈秒時間也沒有任何一個重複。

### 結論

- **可以去重複。** 重送的資料被擋掉，重試也會停下來，不會無限重送。
- **成本：** 每秒 5,000 行時，ES 的 CPU 從 0.44 核升到 0.53 核，多約兩成。原因是寫入前要先查 `_id` 是否已存在。延遲沒有變差。
- **不會誤丟資料：** 內容相同的 log 行也會有不同的 `_id`。
- **已經加進正式設定。** 理由是重複會讓 AIOps 算出的錯誤率、事件數偏高，而 ES 逾時、停機恢復這類重送在正式環境一定會發生。
- **限制：** 去重只在同一個底層 index 內有效。data stream 每天或 30GB 換新 index，如果第一次寫入和重送剛好跨過換 index 的時間點，還是會多一份。機率很低。
- 上表的「強迫重送」那一列，ES CPU 74% 是去重複加上重送本身的額外工作，不是正常情況下的成本。

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

### 3. 大流量時同一批資料被重複寫入，而且一直重送

**現象：** 每秒 5,000 行時，ES 收到的比送出還多 4,964 筆。查下去：
- 序號 439,390 開始連續 2,482 筆，每筆有 4 份。
- 隔了幾分鐘再查，又多了一份，代表還在繼續重送。

**原因：**
- es output 設了 `buffer_size: 512KB`，這是 Fluent Bit **讀取 ES 回應**的上限。
- 流量大時一批 bulk 比較大，ES 的回應超過 512KB，Fluent Bit 讀不完就當成失敗（log 裡是 `cannot increase buffer` 和 `http_do=-1`），整批重送。
- 但 ES 其實已經寫入了，所以每重送一次就多一份。
- 再加上 `retry_limit: no_limits`，這批每次回應都超過上限，就會無限重送下去。

**修正：** `buffer_size: false`（不限制）。修正後重跑 5,000 行，重複數為 0，重試次數也是 0。

### 4. 新 Pod 量大時，開頭的 log 整段遺失

**現象：** 修好問題 3 後重跑 5,000 行，少了 24,100 筆。依序號查，**遺失的剛好是第 1 到第 24,100 行**，中間和結尾都完整。

**原因：**
- kubelet 預設每個 container 的 log 檔寫到 10MB 就輪替並壓縮成 `.gz`，Fluent Bit 不讀 `.gz`。
- Fluent Bit 每 10 秒才掃一次有沒有新的 log 檔（`refresh_interval: 10`）。
- 每秒約 4,800 行、每行約 420 bytes，**約 5 秒就寫滿 10MB**。所以新 Pod 的第一個檔，在 Fluent Bit 發現它之前就被輪替壓縮掉了。
- 24,100 行 × 420 bytes ≈ 10MB，數字對得上。
- 每秒 500、2,000 行時寫滿 10MB 要 20 秒以上，所以沒事。第一輪 5,000 行沒漏，是剛好碰上 Fluent Bit 掃描的時機。

**修正：**
- `refresh_interval` 改成 2 秒。
- 正式環境建議**平台端把 kubelet 的 `container-log-max-size` 調大**，例如 50Mi。RKE2 是在 config 的 `kubelet-arg` 加 `container-log-max-size=50Mi`。這樣量大的服務也有足夠緩衝，Fluent Bit 重啟或短暫卡住時也不會漏。

### 5. ES 恢復後，補送要等好幾分鐘

**現象：** ES 停 3 分鐘、恢復之後，堆積的資料花了 5 分 15 秒才全部補完。期間有一段時間看起來像資料遺失。

**原因：**
- Fluent Bit 每次重試失敗，下次的間隔會加倍，預設上限是 2,000 秒（約 33 分鐘）。
- 停機 3 分鐘時，有些 chunk 已經失敗 5、6 次，下一次重試被排到好幾分鐘後。所以 ES 早就恢復了，它還在等。
- 停機越久，等越久。停 30 分鐘的話，恢復後可能還要再等半小時才補完。

**修正：** `service` 加上 `scheduler.cap: 60`，重試間隔最長 60 秒。

### 6. 測試環境本身的問題（跟設計無關）

- **測試 ES 起來後是 red，發 key 卡住。** 測試機和別人共用磁碟，使用率 92%，超過 ES 預設 90% 的水位線，所以 shard 配置不出去。測試用的 ES 已關掉磁碟水位檢查；正式環境不要這樣做。
- **`elasticsearch-setup.sh` 遇到 ES 沒回應時會一直等。** 已加上 60 秒逾時，ES 有問題會直接失敗，而不是卡住。

## 還沒測的

- 真實環境：你們的 Fluent Bit 版本、中央 ES 的 TLS 憑證、Rancher 上的部署流程。
- 多節點：測試只有一個節點，沒有測多個 Fluent Bit 同時寫入、ES 多節點的情況。
- 長時間：每段只跑 2 到 7 分鐘，沒有跑過 ILM 滾動（每天或 30GB）的情況。

## 附錄：測資樣本（實際抓取）

以下都是從測試環境實際抓下來的，不是手寫的範例。

- **原始 log 行**：取自節點上的 `/var/log/pods/.../0.log`，也就是 Fluent Bit 讀到的東西。前面是 container runtime 加上的奈秒時間、stdout 或 stderr、`F`（完整一行）。
- **ES 文件**：該行進 ES 後實際存成的樣子。有些省略了跟該情境無關的欄位。

### A. 三個常駐測試服務（自動測試、label 情境用）

**`it-mes-prod`（logback JSON，ERROR 帶 stack trace）**

原始 log 行：

```
2026-10-08T13:30:24.781454536Z stdout F {"timestamp":"2026-10-08T13:30:24.000Z","level":"ERROR","message":"order 2350 failed","logger":"com.spil.mes.OrderService","thread":"http-nio-8080-exec-2","stack_trace":"java.lang.IllegalStateException: lot not found\n\tat com.spil.mes.OrderService.run(OrderService.java:42)","trace_id":"5bf92f3577b34da6a3ce929d0e0e4736"}
```

進 ES 後（完整文件，只省略 `kubernetes.labels`）：

```json
{
  "_index": ".ds-k8s-it-local-2026.10.08-000001",
  "_id": "b1566c7b-df1b-9c13-b26c-09d6165b81da",
  "_source": {
    "container": {
      "image": {
        "name": "docker.io/library/busybox:1.37"
      },
      "id": "19e4e00e1faec59c945eba3aeaab6127d72d0ee3dc84e8ce4cb892bb0a911de1"
    },
    "kubernetes": {
      "container": {
        "name": "mes-api"
      },
      "node": {
        "name": "vm"
      },
      "pod": {
        "uid": "19a503c6-00ba-4cdd-8193-f27cfcf9b6ff",
        "name": "mes-api-59fddf95f6-pq46x"
      },
      "namespace": "it-mes-prod"
    },
    "process": {
      "thread": {
        "name": "http-nio-8080-exec-2"
      }
    },
    "log": {
      "level": "error",
      "logger": "com.spil.mes.OrderService"
    },
    "message": "order 2350 failed",
    "error": {
      "stack_trace": "java.lang.IllegalStateException: lot not found\n\tat com.spil.mes.OrderService.run(OrderService.java:42)",
      "type": "java.lang.IllegalStateException"
    },
    "labels": {
      "dept": "D3120",
      "group": "it"
    },
    "orchestrator": {
      "cluster": {
        "name": "local"
      }
    },
    "trace": {
      "id": "5bf92f3577b34da6a3ce929d0e0e4736"
    },
    "@timestamp": "2026-10-08T13:30:24.000Z",
    "stream": "stdout",
    "service": {
      "name": "mes-api",
      "namespace": "mes",
      "version": "2.3.1"
    },
    "event": {
      "ingested": "2026-10-08T13:30:25.720902543Z"
    }
  }
}
```

同一個服務的 INFO 行，自帶的 `lot_id` 收進 `app`：

```json
{
  "_index": ".ds-k8s-it-local-2026.10.08-000001",
  "_id": "b7e51412-6c90-1eb0-929c-73c6c42a2342",
  "_source": {
    "@timestamp": "2026-10-08T13:30:26.000Z",
    "message": "order 2351 accepted",
    "log": {
      "level": "info",
      "logger": "com.spil.mes.OrderService"
    },
    "app": {
      "lot_id": "L2351"
    },
    "labels": {
      "dept": "D3120",
      "group": "it"
    },
    "service": {
      "name": "mes-api",
      "namespace": "mes",
      "version": "2.3.1"
    }
  }
}
```

**`imc-wip-prod`（ECS 巢狀 JSON）**

原始 log 行：

```
2026-10-08T13:30:20.436189884Z stdout F {"@timestamp":"2026-10-08T13:30:20.000Z","log":{"level":"WARN","logger":"wip.Queue"},"process":{"pid":1,"thread":{"name":"main"}},"message":"queue depth high","ecs":{"version":"8.11"}}
```

進 ES 後。`log.level`、`log.logger`、thread 被拆出來；沒用到的 `process.pid`、`ecs.version` 留在 `app`：

```json
{
  "_index": ".ds-k8s-imc-local-2026.10.08-000001",
  "_id": "2554b5bb-f0cd-8c29-98be-91b5b3cde49a",
  "_source": {
    "@timestamp": "2026-10-08T13:30:26.000Z",
    "message": "queue depth high",
    "log": {
      "level": "warn",
      "logger": "wip.Queue"
    },
    "process": {
      "thread": {
        "name": "main"
      }
    },
    "app": {
      "process": {
        "pid": 1
      },
      "ecs": {
        "version": "8.11"
      }
    },
    "labels": {
      "dept": "D1110",
      "group": "imc"
    },
    "service": {
      "name": "wip-worker",
      "namespace": "wip",
      "version": "1.0.4"
    }
  }
}
```

**`legacy`（純文字、沒標 dept、沒套 chart）**

原始 log 行：

```
2026-10-08T13:30:21.257066056Z stdout F plain text Thu Oct  8 13:30:21 UTC 2026
```

進 ES 後。進 unassigned，`message` 是原文，`service.name` 退回 container 名稱：

```json
{
  "_index": ".ds-k8s-unassigned-local-2026.10.08-000001",
  "_id": "ec3ec74c-f521-df24-ebcf-2eef314c6dde",
  "_source": {
    "container": {
      "image": {
        "name": "docker.io/library/busybox:1.37"
      },
      "id": "a2281eacd1e68ffa7703eb379cf0cbd1d566d4c7d41dbc6b745556e08658528a"
    },
    "orchestrator": {
      "cluster": {
        "name": "local"
      }
    },
    "kubernetes": {
      "container": {
        "name": "legacy"
      },
      "node": {
        "name": "vm"
      },
      "pod": {
        "uid": "b701dd23-d4e9-4764-8314-f7a4b5b09758",
        "name": "legacy-5c7588d6c5-wrnxj"
      },
      "namespace": "legacy",
      "labels": {
        "app": "legacy",
        "pod-template-hash": "5c7588d6c5"
      }
    },
    "@timestamp": "2026-10-08T13:30:27.267Z",
    "stream": "stdout",
    "service": {
      "name": "legacy"
    },
    "message": "plain text Thu Oct  8 13:30:27 UTC 2026",
    "event": {
      "ingested": "2026-10-08T13:30:27.720083661Z"
    },
    "labels": {
      "group": "unassigned"
    }
  }
}
```

**ES 自己的 log（ECS 點號寫法）**

原始 log 行：

```
2026-10-08T13:13:22.822671286Z stdout F {"@timestamp":"2026-10-08T13:13:22.807Z", "log.level": "INFO", "message":"vec_caps=2", "ecs.version": "1.2.0","service.name":"ES_ECS","event.dataset":"elasticsearch.server","process.thread.name":"main","log.logger":"org.elasticsearch.nativeaccess.jdk.JdkVectorLibrary","elasticsearch.node.name":"elasticsearch-0","elasticsearch.cluster.name":"docker-cluster"}
```

進 ES 後。`app` 只列出有哪些 key：

```json
{
  "_index": ".ds-k8s-unassigned-local-2026.10.08-000001",
  "_source": {
    "app": {
      "elasticsearch.cluster.name": "…",
      "reason": "…",
      "service.name": "…",
      "current.health": "…",
      "previous.health": "…",
      "elasticsearch.cluster.uuid": "…",
      "elasticsearch.node.name": "…",
      "ecs.version": "…",
      "event.dataset": "…",
      "elasticsearch.node.id": "…"
    },
    "orchestrator": {
      "cluster": {
        "name": "local"
      }
    },
    "process": {
      "thread": {
        "name": "elasticsearch[elasticsearch-0][masterService#updateTask][T#6]"
      }
    },
    "@timestamp": "2026-10-08T13:28:11.812Z",
    "stream": "stdout",
    "log": {
      "level": "info",
      "logger": "org.elasticsearch.cluster.routing.allocation.AllocationService"
    },
    "service": {
      "name": "elasticsearch"
    },
    "message": "Cluster health status changed from [YELLOW] to [GREEN] (reas…",
    "event": {
      "ingested": "2026-10-08T13:28:12.721426904Z"
    },
    "labels": {
      "group": "unassigned"
    }
  }
}
```

### B. dept label 情境

同一個 `legacy` 服務，`kubectl label namespace legacy dept=D3120` 前後的文件。

改之前（同上面 A 的 `legacy`）：進 `k8s-unassigned-local`，`labels` 只有 `group: unassigned`。

改之後：進 `k8s-it-local`，帶上 dept 和 group：

```json
{
  "_index": ".ds-k8s-it-local-2026.10.08-000001",
  "_source": {
    "kubernetes": {
      "namespace": "legacy"
    },
    "@timestamp": "2026-10-08T13:31:00.319Z",
    "service": {
      "name": "legacy"
    },
    "message": "plain text Thu Oct  8 13:31:00 UTC 2026",
    "labels": {
      "dept": "D3120",
      "group": "it"
    }
  }
}
```

這份樣本是為了抓樣本另外重現的：改完 label 直接重啟 Fluent Bit，所以馬上生效。正式的測試沒有重啟，是 13 分 45 秒後才生效。

### C. 大流量、ES 停機情境（同一個產生器）

兩個情境用同一個產生器，差別只在速率和 `run` 名稱：

| 情境 | 速率（行/秒） | `run` |
|---|---|---|
| 大流量 | 500、2,000、5,000 | `r500-…`、`r2000-…`、`r5000-…` |
| ES 停機 | 100 | `outage-…` |

下面是用每秒 5 行、跑 2 秒抓的樣本，`run=sample`。

原始 log 行。最後一行是產生器結束時寫到 stderr 的送出筆數，統計時用它當「送出」：

```
2026-10-08T13:31:21.215859937Z stdout F {"timestamp": "2026-10-08T13:31:21.214Z", "level": "INFO", "message": "load xxxx…(250 個 x)", "logger": "load.Gen", "thread": "main", "run": "sample", "seq": 1}
2026-10-08T13:31:21.415553835Z stdout F {"timestamp": "2026-10-08T13:31:21.414Z", "level": "INFO", "message": "load xxxx…(250 個 x)", "logger": "load.Gen", "thread": "main", "run": "sample", "seq": 2}
2026-10-08T13:31:21.665417842Z stdout F {"timestamp": "2026-10-08T13:31:21.664Z", "level": "INFO", "message": "load xxxx…(250 個 x)", "logger": "load.Gen", "thread": "main", "run": "sample", "seq": 3}
2026-10-08T13:31:23.025483326Z stderr F {"run": "sample", "emitted": 9, "seconds": 2.0}
```

進 ES 後。`seq` 在 `app` 裡，統計遺失和重複都靠它：

```json
{
  "_index": ".ds-k8s-imc-local-2026.10.08-000001",
  "_id": "69492b47-c0bc-fa69-3b76-281c2151497c",
  "_source": {
    "app": {
      "run": "sample",
      "seq": 1
    },
    "kubernetes": {
      "namespace": "load-test"
    },
    "@timestamp": "2026-10-08T13:31:21.214Z",
    "log": {
      "level": "info",
      "logger": "load.Gen"
    },
    "service": {
      "name": "load-gen",
      "version": "1.0.0"
    },
    "message": "load xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx…",
    "labels": {
      "dept": "D1110",
      "group": "imc"
    }
  }
}
```

### D. 去重複情境

**相同內容測試**：一口氣輸出完全相同的 JSON。下面用 5 行當樣本，正式測試是 10,000 行。

原始 log 行。內容一樣，但 container runtime 給的奈秒時間都不同：

```
2026-10-08T13:31:22.288243491Z stdout F {"level":"INFO","message":"heartbeat ok","run":"same-lines"}
2026-10-08T13:31:22.28829544Z stdout F {"level":"INFO","message":"heartbeat ok","run":"same-lines"}
2026-10-08T13:31:22.288333319Z stdout F {"level":"INFO","message":"heartbeat ok","run":"same-lines"}
2026-10-08T13:31:22.288336363Z stdout F {"level":"INFO","message":"heartbeat ok","run":"same-lines"}
2026-10-08T13:31:22.288337472Z stdout F {"level":"INFO","message":"heartbeat ok","run":"same-lines"}
```

進 ES 後。5 筆都在，時間到毫秒都一樣，但 `_id` 都不同，所以沒有被當成重複：

```
{"_id":"e52a01ab-a452-3621-7885-e5121def993f","ts":"2026-10-08T13:31:22.288Z","message":"heartbeat ok"}
{"_id":"bd03ef89-e721-ca01-af4e-2c8af90a1cbd","ts":"2026-10-08T13:31:22.288Z","message":"heartbeat ok"}
{"_id":"fc67623e-06ad-74d3-cd1e-ef9f4035285a","ts":"2026-10-08T13:31:22.288Z","message":"heartbeat ok"}
{"_id":"0d7f6973-0628-c3e2-7b7e-fb32075a2b89","ts":"2026-10-08T13:31:22.288Z","message":"heartbeat ok"}
{"_id":"dca15494-bc73-6841-ead8-07c684d8cc95","ts":"2026-10-08T13:31:22.288Z","message":"heartbeat ok"}
```

**強迫重送測試**的測資跟 C 一樣，只是 Fluent Bit 換成測試專用設定。

### E. 依格式分派

**`hold-svc`（宣告 `logfmt`）**

原始 log 行：

```
2026-10-08T15:12:50.159008804Z stdout F ts=2026-10-08T15:12:50.000Z level=warn msg="lot hold requested" lot_id=L64 caller=hold.go:42
```

進 ES 後。`log.format` 和 `log.parser` 都是 logfmt；`level`、`msg`、`ts` 對應到標準欄位，其餘收進 `app`：

```json
{
  "_index": ".ds-k8s-it-local-2026.10.08-000001",
  "_source": {
    "app": {
      "caller": "hold.go:42",
      "lot_id": "L64"
    },
    "@timestamp": "2026-10-08T15:12:50.000Z",
    "log": {
      "parser": "logfmt",
      "level": "warn",
      "format": "logfmt"
    },
    "service": {
      "name": "hold-svc",
      "version": "0.9.0"
    },
    "message": "lot hold requested",
    "labels": {
      "dept": "D3120",
      "group": "it"
    }
  }
}
```

**`eap`（宣告 `secs`，中央還沒有規則）**

原始 log 行：

```
2026-10-08T15:12:50.158398818Z stdout F 2026-10-08 15:12:50 [EQP01] RECV S6F11 W SystemBytes=1234
```

進 ES 後。`log.format` 是 secs、`log.parser` 是 default，代表退回了預設規則；`message` 是完整原文：

```json
{
  "_index": ".ds-k8s-it-local-2026.10.08-000001",
  "_source": {
    "@timestamp": "2026-10-08T15:12:50.158Z",
    "log": {
      "parser": "default",
      "format": "secs"
    },
    "service": {
      "name": "eap",
      "version": "3.1.0"
    },
    "message": "2026-10-08 15:12:50 [EQP01] RECV S6F11 W SystemBytes=1234",
    "labels": {
      "dept": "D3120",
      "group": "it"
    }
  }
}
```

### F. key 失效、權限情境

ES 實際回的錯誤。

IT 的 key 寫 IMC 的 index（權限不足，403）：

```json
{"status":403,"type":"security_exception","reason":"action [indices:data/write/bulk[s]] is unauthorized for API key id [AUmyG6EBNBd9KyJ725FB] of user [elastic] on indices [k8s-imc-local], this action is granted by the index privileges [create_doc,create,delete,index,write,all]"}
```

作廢或錯誤的 key（認證失敗，401）。key 失效那段時間，Fluent Bit 每次送出都會收到這個：

```json
{"status":401,"type":"security_exception","reason":"unable to authenticate with provided credentials and anonymous access is not allowed for this request"}
```

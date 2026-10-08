# Log 進 Elasticsearch

一句話：**Fluent Bit 只負責「收」和「分流」，欄位整理一律交給中央 ES 的 pipeline。** 規則改在中央，不用動每個 cluster。

> [!IMPORTANT]
> **重點：log 格式不一致的處理，見下方「[pipeline](#pipeline)」。**
> 依服務宣告的格式分派解析規則的機制已經完成並測過；**SECS、REST API 的規則還沒寫，等真實 log 樣本。**

## 資料怎麼走

```
container stdout（每個節點的 /var/log/containers）
        ↓ Fluent Bit DaemonSet：補上 pod label、namespace label、cluster
        ↓ 依 namespace 的 dept 分流（第一條符合的規則決定去向）
   ├─ dept 屬於 IT    → key: IT_ES_API_KEY         → k8s-it-{cluster}
   ├─ dept 屬於 IMC   → key: IMC_ES_API_KEY        → k8s-imc-{cluster}
   └─ 其他（兜底）     → key: UNASSIGNED_ES_API_KEY → k8s-unassigned-{cluster}
        ↓ 中央 ES：k8s-logs pipeline 整理欄位（依 log 格式分派）→ data stream（ILM 管保留天數）
        ↓
   AIOps 用唯讀 key 讀 k8s-*；人用各 group 的唯讀角色
```

跟 metrics 那側（[vmagent.md](vmagent.md)）是同一套思路：收一次、每個 group 一條出口、一把 key、一個獨立佇列。

不一樣的地方：**分流規則是依序比對、第一條符合就停**，所以不會一筆送兩份。metrics 那側的 keep/drop regex 要自己確保互斥，這裡不用。

## 檔案

| 檔案 | 用途 |
|---|---|
| [platform/groups.yaml](../platform/groups.yaml) | 部門 → group 對照表 |
| [platform/central/elasticsearch-setup.sh](../platform/central/elasticsearch-setup.sh) | 中央 ES：index 結構＋各種 API key 與角色 |
| [platform/central/elasticsearch/](../platform/central/elasticsearch) | ILM、mapping、pipeline、index template |
| [platform/central/elasticsearch/formats/](../platform/central/elasticsearch/formats) | 各 log 格式的解析規則，一種格式一個檔 |
| [platform/cluster-addons/fluent-bit/](../platform/cluster-addons/fluent-bit) | 每個應用 cluster 的 Fluent Bit（kustomize） |
| [test/local-k3s/](../test/local-k3s) | 端到端測試 |

## 欄位規範

AIOps 只認這些欄位。標準概念用 [ECS](https://www.elastic.co/guide/en/ecs/current/index.html) 名稱，跟 OpenTelemetry 的命名對得上。

| 欄位 | 來源 | AIOps 拿來做什麼 |
|---|---|---|
| `@timestamp` | 應用 log 裡的時間；沒有就用 Fluent Bit 收到的時間 | 時間軸 |
| `message` | 應用的訊息；不是 JSON 就是整行原文 | log 樣板分群 |
| `log.level` | 應用的 level，一律轉小寫。應用沒寫的話，從原文行首附近抓 `ERROR`、`WARN` 這類關鍵字 | 錯誤率 |
| `log.level_inferred` | `true` 表示 `log.level` 是從原文猜的，不是應用自己寫的 | 分辨可信度 |
| `log.logger`、`process.thread.name` | 應用 | 縮小範圍 |
| `error.type` | 從 stack trace 第一行拆出的例外類別 | 例外分群，比對全文便宜很多 |
| `error.stack_trace` | 應用 | 人看 |
| `trace.id`、`span.id` | OpenTelemetry Java agent 寫進 MDC | 串起同一個請求的 log |
| `service.name` | pod label `app.kubernetes.io/name`；沒有就用 container 名稱 | 分析單位 |
| `service.version` | pod label `app.kubernetes.io/version` | 「換版之後才開始錯」 |
| `service.namespace` | pod label `app.kubernetes.io/part-of` | 所屬系統 |
| `labels.group`、`labels.dept` | 分流結果、namespace label | 歸屬 |
| `orchestrator.cluster.name` | Fluent Bit 的 `CLUSTER_NAME` | 哪一廠 |
| `kubernetes.namespace`、`.pod.name`、`.node.name`、`.container.name` | K8s | 對回 metrics |
| `kubernetes.labels` | 全部 pod label（flattened） | 備用 |
| `app` | 應用自帶的其他欄位，例如 MDC 的 `lot_id`（flattened） | 業務維度 |
| `log.format` | 服務宣告的 log 格式（pod label `log-format`），沒宣告是 `default` | 追蹤各格式的覆蓋率 |
| `log.parser` | 實際用來解析的規則。和 `log.format` 不同，代表那個格式還沒有規則，退回 default | 找出還沒寫規則的格式 |
| `event.ingested` | ES 收到的時間 | 算延遲 |

和 metrics 的 label 對應：`service.name` ↔ `app.kubernetes.io/name`、`kubernetes.namespace` ↔ `namespace`、`labels.group` ↔ `group`、`labels.dept` ↔ `dept`。

目前認得的 log 格式見下一節「pipeline」。

**為什麼自帶欄位收進 `app` 而不是攤平在最上層：** 幾百個服務共用一個 index，每個服務多幾個 MDC 欄位，欄位數很快就撞到上限，整個 index 拒收。`flattened` 型別只佔一個欄位，仍然可以用 `app.lot_id: L123` 查詢。

## pipeline

pipeline 是 **Elasticsearch 內建的「寫入前加工」**（ingest pipeline）。Fluent Bit 送來的資料，在存進 index 之前，先跑過一串處理步驟整理好再存。

例如一筆 logback JSON：

```
送進來：  log = "{\"level\":\"ERROR\",\"message\":\"order 5 failed\",\"stack_trace\":\"java.lang.IllegalStateException: ...\"}"
          kubernetes.labels = {app.kubernetes.io/name: mes-api, app.kubernetes.io/version: 2.3.1}
存進去：  message = "order 5 failed"、log.level = "error"、error.type = "java.lang.IllegalStateException"
          service.name = "mes-api"、service.version = "2.3.1"、labels.group = "it"…
```

**為什麼放在中央 ES，不放在 Fluent Bit：** Fluent Bit 每個 cluster 都有一套，規則放那裡，每改一次就要每廠重新部署。放在中央只要改一處，跑一次 `elasticsearch-setup.sh` 就全廠生效。

> [!IMPORTANT]
> **這一節是 log 端的重點。** 格式拆不出欄位，AIOps 就只能看到一串文字。
>
> | 狀態 | 項目 |
> |---|---|
> | ✅ 完成 | 依格式分派的機制、`default`（JSON／純文字）、`logfmt`、沒有規則時退回並保留原文、`log.format`／`log.parser` 追蹤、純文字保底抓 level |
> | ⏳ 待辦 | **SECS**：解析規則＋Fluent Bit 多行合併。需要真實樣本：一般事件（S6F11）、告警（S5F1）、逾時（S9F9）、多行 body，以及 EAP 用的軟體或函式庫 |
> | ⏳ 待辦 | **REST API**：只統一 method、路徑（換成樣板）、狀態碼、耗時、trace_id，body 不統一。需要兩三個服務的 log 範例，以及有沒有 ingress／API gateway 的存取紀錄 |

### 結構：共通身分 → 依格式分派 → 欄位對應

log 格式不可能全廠統一，例如 Java 的 JSON、Go 的 key=value、設備通訊的 SECS、各家 REST API 自己的格式。所以分三層：

```
k8s-logs（主 pipeline）
  1. 共通身分：服務、版本、group、dept、cluster、K8s 資訊        ← 跟 log 內容長怎樣無關，每筆一定有
     原文先放進 message
  2. 看 pod label log-format，交給 k8s-logs-format-<格式>
       ├─ default（沒宣告）：JSON 就拆，不是 JSON 就保留原文
       ├─ logfmt：level=info msg="..." key=value
       └─ 宣告了但中央還沒有這個格式的規則 → 退回 default，原文完整保留
     各格式只負責「把原文拆成欄位」，拆出來的交給 ↓
     k8s-logs-fields：對應成標準欄位（level/lvl → log.level、msg → message…），所有格式共用
                      還是沒有 level 的，從原文行首附近抓全大寫的 ERROR/WARN/INFO…（保底）
  3. 時間改用應用自己的時間；蓋上 ES 收到的時間
  任何一步出錯：原文照樣入庫，錯誤寫在 error.message，不丟資料
```

### 保底：沒有規則的格式也盡量抓 level

不管什麼格式，只要最後還沒有 `log.level`，就從原文**前 80 個字元**找**獨立、全大寫**的 level 關鍵字：

| 關鍵字 | `log.level` |
|---|---|
| `TRACE` | trace |
| `DEBUG` | debug |
| `INFO` | info |
| `WARN`、`WARNING` | warn |
| `ERROR`、`ERR` | error |
| `FATAL`、`CRITICAL`、`CRIT` | fatal |

- 找到的話設 `log.level`，並標 `log.level_inferred: true`。
- 應用自己寫了 level 的不會被覆蓋。
- 為了少誤判，下面這些**不算**：
  - 小寫：`no error found`。
  - 連在別的字上：`ERROR_CODE=5`。
  - 在第 80 個字元之後。
- 仍然可能誤判，例如訊息開頭剛好是 `NO ERROR`。所以 AIOps 對 `log.level_inferred: true` 的資料要降低權重。

這樣一來，就算一個服務的格式完全沒有對應規則，AIOps 至少還能算它的錯誤率。

**目前的格式：**

| `log-format` | 給誰用 | 例子 |
|---|---|---|
| （不填） | Java 標準（logback JSON）、ECS JSON（Spring Boot 3.4 `logging.structured.format.console=ecs`）、純文字 | `{"level":"INFO","message":"..."}` |
| `logfmt` | Go 等語言常見的 key=value | `level=warn msg="lot hold requested" lot_id=L123` |

### 服務怎麼宣告格式

用 app-common chart 的服務，在 values 填 `logFormat`，chart 會在 pod 加上 label `log-format`：

```yaml
logFormat: logfmt
```

- 名稱只能是小寫英數與減號，填錯部署直接失敗。
- 照 Java 標準輸出的服務不用填。
- 沒用 chart 的服務，直接在 pod template 加 label `log-format: <格式>`。

### 新增一種格式（例如 SECS）

1. 在 [formats/](../platform/central/elasticsearch/formats) 加 `<格式>.json`。只要做到「原文 → `_app`」，再交給 `k8s-logs-fields`。可以參考 [logfmt.json](../platform/central/elasticsearch/formats/logfmt.json)。需要寫程式的話，放同名的 `<格式>.painless`。
2. 規則裡第一步一定要 `set log.parser = <格式>`，解析失敗時不能丟掉原文（`ignore_failure` 或自己的 `on_failure`）。
3. 在 [test/local-k3s/](../test/local-k3s) 加一個輸出該格式的測試服務和檢查項目，跑 `run-e2e.sh`。
4. 跑 `elasticsearch-setup.sh`（`SKIP_KEYS=1`）裝到中央。**各 cluster 的 Fluent Bit 不用動。**
5. 服務在 values 填上 `logFormat`。

可以**先讓服務宣告格式、之後再補規則**：還沒有規則的期間，log 照收、原文完整，只是沒拆欄位。規則裝上去之後，新進來的資料就會拆。

**例外：一則訊息跨很多行的格式**（例如 SECS 的 SML body）。多行要在 Fluent Bit 收的時候就合併成一筆，送到中央就拼不回來了，所以這種格式除了第 1 步，還要在 Fluent Bit 加多行合併的規則。

### 查哪些格式還沒有規則

```
GET k8s-*/_search
{"size": 0, "query": {"bool": {"must_not": {"term": {"log.format": "default"}}}},
 "aggs": {"format": {"terms": {"field": "log.format"}, "aggs": {"parser": {"terms": {"field": "log.parser"}}}}}}
```

`log.format` 是 `secs`、`log.parser` 卻是 `default` 的，就是宣告了但還沒寫規則。

## Index

- 命名：`k8s-{group}-{cluster}`。每個 group、每個 cluster 一條 data stream。
  - 權限照 group 切，所以名稱要帶 group；這就是現有寫入 key 的 `k8s-it-*` 樣式。
  - 名稱帶 cluster，某一廠出狀況要整批清掉或調保留天數時，不會動到別廠。
- `CLUSTER_NAME` 要小寫，ES 的 index 名稱不接受大寫。
- shard：每條 data stream 1 個 primary，每天或 30GB 滾一次。
- 保留天數：先暫定 30 天，見 [ilm-policy.json](../platform/central/elasticsearch/ilm-policy.json)。
- 這套用到的功能都在免費授權（Basic）內：API key、ILM、data stream、flattened。

## 部署

### 1. 中央 ES（一次）

```bash
ES_URL=https://<es>:9200 ES_AUTH=elastic:<password> platform/central/elasticsearch-setup.sh
```

輸出裡每把 key 的 `encoded` 要記下來。只更新 index 結構、不發新 key 時，加上 `SKIP_KEYS=1`。

### 2. 每個應用 cluster

```bash
kubectl create namespace logging
kubectl -n logging create secret generic es-writer-keys \
  --from-literal=IT_ES_API_KEY=<encoded> \
  --from-literal=IMC_ES_API_KEY=<encoded> \
  --from-literal=UNASSIGNED_ES_API_KEY=<encoded>
```

在 Rancher 上做的注意事項跟 vmauth token 一樣，見 [vmagent.md 的「改 Secret 的坑」](vmagent.md#改-secret-的坑)。

改好 `platform/cluster-addons/fluent-bit/kustomization.yaml` 裡的 `CLUSTER_NAME`、`ES_HOST` 和 image 路徑，然後：

```bash
kubectl apply -k platform/cluster-addons/fluent-bit
```

### 3. namespace 標上 dept

```bash
kubectl label namespace <ns> dept=D3120
```

Fluent Bit 把 namespace label 快取 15 分鐘，**標了之後最多 15 分鐘才會生效**；在那之前的資料會進 unassigned。

## 驗證

```bash
test/local-k3s/run-e2e.sh
```

在單節點 k3s 上跑過（ES 8.17、Fluent Bit 4.1.1），49 項全部通過。另外跑過幾個情境測試：dept label 改了多久生效、每秒最多 5,000 行的大流量、ES 整台停機、寫入去重複。測資、每一項結果和故障測試的完整紀錄見 [elasticsearch-testing.md](elasticsearch-testing.md)。

- **分流**：三個 namespace 各自只進自己的 index，沒有重複送。
- **欄位**：logback 格式、ECS 巢狀格式、ECS 點號格式（ES 自己的 log）、純文字，四種都正確轉換。
- **依格式分派**：宣告 `logfmt` 的服務走 logfmt 規則；宣告了還沒有規則的 `secs`，退回 default，原文完整保留。
- **保底抓 level**：純文字裡的 `ERROR`、`[WARNING]`、`INFO:` 都抓得到；小寫的 error、`ERROR_CODE` 不會被誤抓；應用自己寫了 level 的不會被覆蓋。
- **權限**：寫入 key 只能寫自己的 group，不能讀；AIOps key 能讀全部 k8s-*，不能寫。

另外手動測過一把 key 失效的情況（不在腳本裡）：

- IMC 的 key 作廢 45 秒，IT 照常寫入，只有 IMC 停住。
- 換上新 key、重啟 Fluent Bit 後，失效期間的 15 筆全部補送進來，沒有遺失。

正式上線後，用 AIOps key 查一次，確認每個 namespace 只落在一個 group：

```
GET k8s-*/_search
{"size": 0, "aggs": {"ns": {"terms": {"field": "kubernetes.namespace", "size": 500},
  "aggs": {"group": {"terms": {"field": "labels.group"}}}}}}
```

## 坑

- **Fluent Bit 要 4.1 以上。** es output 從 4.1 才有 `http_api_key`，舊版只能用帳號密碼，這樣 setup 腳本發的 key 就用不上。`namespace_labels` 則是 3.x 就有。
- **Fluent Bit 不能收自己的 log。** ES 拒收時它會印錯誤；如果再收回來送一次，就是放大迴圈。實測過：一次 key 設錯，十幾分鐘就灌進一萬七千多筆。設定裡已經排除，不要拿掉。`trace_error` 平常也保持關閉。
- **換 key 一定要重啟 Fluent Bit**，它只在啟動時讀環境變數：
  ```bash
  kubectl -n logging rollout restart daemonset/fluent-bit
  ```
- **key 失效或 ES 停機期間，資料會堆在節點上**，每條出口上限 2G，放在 `/var/fluent-bit/state`，超過上限就丟最舊的。實測每秒 100 行停 3 分鐘約堆 25MB，所以每秒 100 行可以撐約 4 小時、每秒 1,000 行約 25 分鐘。
- **ES 回應的讀取上限（`buffer_size`）一定要是 `false`。** 設成固定大小時，流量一大就會整批重複寫入、無限重送。
- **掃新 log 檔的間隔（`refresh_interval`）不能太長。** kubelet 預設 log 檔 10MB 就輪替壓縮，量大的新 Pod 在被發現前，第一個檔可能就沒了。目前設 2 秒。平台端建議把 kubelet 的 `container-log-max-size` 調大，例如 50Mi。
- **重試間隔上限（`scheduler.cap`）設 60 秒。** 用預設的 2,000 秒時，ES 停 3 分鐘、恢復後要 5 分多鐘才補完；改了之後約 1 分鐘。
- **`generate_id: on` 防止重送造成重複**，代價是 ES 的 CPU 多約兩成。
- **平台元件的 log 都進 unassigned**，例如 kube-system 和 Fluent Bit 以外的系統元件。這跟 metrics 的兜底 tenant 同一個意思。
- **純文字的 Java stack trace 會被拆成很多行。** 標準格式是 JSON（stack trace 在一個欄位裡），所以不處理。沒照標準的服務，AIOps 在它身上的效果會比較差。
- **節點用 Docker 當 runtime 的 cluster 要另外掛 `/var/lib/docker/containers`。** RKE2、k3s 是 containerd，不需要。

## 還沒做

1. **K8s events**（OOMKilled、probe 失敗、BackOff）。這是 AIOps 很重要的訊號，但 event 本身只帶 namespace 名稱，不帶 dept；要分流得先決定歸屬放哪裡，見 README 待定事項第 1 點。唯讀角色裡已經預留了 `k8s-events-{group}-*`。
2. **保留天數**要各 group 確認。
3. **⚠️ 重點：SECS、REST API 等格式的解析規則**：架構已經好了（見「[pipeline](#pipeline)」），等拿到真實的 log 樣本再寫。SECS 還要在 Fluent Bit 加多行合併。
4. **dept 清單目前要手動同步三處**：`groups.yaml`、Fluent Bit 規則、vmagent regex。group 變多之前要改成從 `groups.yaml` 產生。

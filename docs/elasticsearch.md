# Log 進 Elasticsearch

一句話：**每個節點的 OpenTelemetry Collector 只負責「收」和「分流」，欄位整理一律交給中央 ES 的 pipeline。** 規則改在中央，不用動每個 cluster。

log 和 metrics 用同一個 Collector（[metrics.md](metrics.md)），分部門的規則、key 都共用。原本用 Fluent Bit，改用 OTel 的原因與比較見 [otel-logs-compare.md](otel-logs-compare.md)。

> [!IMPORTANT]
> **重點：log 格式不一致的處理，見下方「[pipeline](#pipeline)」。**
> 依服務宣告的格式分派解析規則的機制已經完成並測過；**SECS、REST API 的規則還沒寫，等真實 log 樣本。**

## 資料怎麼走

```
container stdout（每個節點的 /var/log/pods）
        ↓ 節點的 OpenTelemetry Collector（DaemonSet，跟 metrics 同一個）：補上 pod label、namespace 的 dept、cluster
        ↓ dept → group（跟 metrics 同一份規則）
   ├─ group it         → key: IT_ES_API_KEY         ─┐
   ├─ group imc        → key: IMC_ES_API_KEY        ─┼→ OTLP 送到 ES 的 /_otlp/v1/logs → logs-generic.otel-{group}
   └─ group unassigned → key: UNASSIGNED_ES_API_KEY ─┘
        ↓ 中央 ES：k8s-logs-otel 整理形狀、用內容算 _id（擋重送的重複）、轉到 k8s-{group}-{cluster}
        ↓          轉過去時 ES 會再檢查一次 key 的權限，IT 的 key 寫不進 IMC
        ↓ 中央 ES：k8s-logs pipeline 整理欄位（依 log 格式分派）→ data stream（ILM 管保留天數）
        ↓
   AIOps 用唯讀 key 讀 k8s-*；人用各 group 的唯讀角色
```

跟 metrics 共用同一組 key 與部門清單：收一次、每個 group 一條出口、一把 key、一個獨立佇列，每筆只會符合一條，不會送兩份。

**中央 pipeline 也接受 Fluent Bit 送的資料**（沒有 `agent.type` 的就當成 Fluent Bit），以後邊緣 VM 之類的地方用 Fluent Bit 也能直接接。每筆的 `agent.type`（`otelcol`／`fluent-bit`）記錄是哪個收集器送的。

## 檔案

| 檔案 | 用途 |
|---|---|
| [platform/groups.yaml](../platform/groups.yaml) | 部門 → group 對照表 |
| [platform/central/elasticsearch-setup.sh](../platform/central/elasticsearch-setup.sh) | 中央 ES：index 結構＋各種 API key 與角色 |
| [platform/central/elasticsearch/](../platform/central/elasticsearch) | ILM、mapping、pipeline、index template |
| [platform/central/elasticsearch/formats/](../platform/central/elasticsearch/formats) | 各 log 格式的解析規則，一種格式一個檔 |
| [platform/central/elasticsearch/pipeline-otel.*](../platform/central/elasticsearch/pipeline-otel.painless)、[logs-otel-custom.json](../platform/central/elasticsearch/logs-otel-custom.json) | `k8s-logs-otel`：OTel 送來的 log 整理形狀、算 `_id`、轉到 `k8s-{group}-{cluster}` |
| [platform/cluster-addons/otel-collector/](../platform/cluster-addons/otel-collector) | 每個應用 cluster 的 OpenTelemetry Collector（log 與 metrics，kustomize）；log 的收法在 `node.yaml` |
| [test/local-k3s/](../test/local-k3s) | 端到端測試 |
| [test/pipeline/](../test/pipeline) | pipeline 案例測試（只需要 ES） |

## 欄位規範

AIOps 只認這些欄位。標準概念用 [ECS](https://www.elastic.co/guide/en/ecs/current/index.html) 名稱，跟 OpenTelemetry 的命名對得上。

| 欄位 | 來源 | AIOps 拿來做什麼 |
|---|---|---|
| `@timestamp` | 應用 log 裡的時間；沒有就用 container runtime 寫下這行的時間 | 時間軸 |
| `message` | 應用的訊息；不是 JSON 就是整行原文 | log 樣板分群 |
| `log.level` | 應用的 level，統一成 trace/debug/info/warn/error/fatal。應用沒寫的話，從原文抓（見「保底」） | 錯誤率 |
| `log.level_inferred` | `true` 表示 `log.level` 是從原文猜的，不是應用自己寫的 | 分辨可信度 |
| `log.logger`、`process.thread.name` | 應用 | 縮小範圍 |
| `error.type` | 從 stack trace 第一行拆出的例外類別 | 例外分群，比對全文便宜很多 |
| `error.stack_trace` | 應用 | 人看 |
| `trace.id`、`span.id` | OpenTelemetry Java agent 寫進 MDC | 串起同一個請求的 log，也對得到 trace 的 `trace_id`（[traces.md](traces.md)） |
| `service.name` | pod label `app.kubernetes.io/name`；沒有就用 container 名稱 | 分析單位 |
| `service.version` | pod label `app.kubernetes.io/version` | 「換版之後才開始錯」 |
| `service.namespace` | pod label `app.kubernetes.io/part-of` | 所屬系統 |
| `labels.group`、`labels.dept` | 分流結果、namespace label | 歸屬 |
| `orchestrator.cluster.name` | Collector 的 `CLUSTER_NAME` | 哪一廠 |
| `agent.type` | 哪個收集器送的：`otelcol` 或 `fluent-bit` | 搬遷時比對、查收集器的問題 |
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

pipeline 是 **Elasticsearch 內建的「寫入前加工」**（ingest pipeline）。收集器送來的資料，在存進 index 之前，先跑過一串處理步驟整理好再存。

例如一筆 logback JSON：

```
送進來：  log = "{\"level\":\"ERROR\",\"message\":\"order 5 failed\",\"stack_trace\":\"java.lang.IllegalStateException: ...\"}"
          kubernetes.labels = {app.kubernetes.io/name: mes-api, app.kubernetes.io/version: 2.3.1}
存進去：  message = "order 5 failed"、log.level = "error"、error.type = "java.lang.IllegalStateException"
          service.name = "mes-api"、service.version = "2.3.1"、labels.group = "it"…
```

**為什麼放在中央 ES，不放在收集器：** 收集器每個 cluster 都有一套，規則放那裡，每改一次就要每廠重新部署。放在中央只要改一處，跑一次 `elasticsearch-setup.sh` 就全廠生效。

> [!IMPORTANT]
> **這一節是 log 端的重點。** 格式拆不出欄位，AIOps 就只能看到一串文字。
>
> | 狀態 | 項目 |
> |---|---|
> | ✅ 完成 | 依格式分派的機制、`default`（JSON／純文字）、`logfmt`、沒有規則時退回並保留原文、`log.format`／`log.parser` 追蹤、純文字保底抓 level |
> | ⏳ 待辦 | **SECS**：解析規則＋收集器多行合併。需要真實樣本：一般事件（S6F11）、告警（S5F1）、逾時（S9F9）、多行 body，以及 EAP 用的軟體或函式庫 |
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

不管什麼格式，只要最後還沒有 `log.level`，就從原文抓。兩條規則，依序：

| 規則 | 條件 | 例子 |
|---|---|---|
| A. 行首前綴 | 一行**最開頭**的字（可包在 `[` `]` 裡），後面緊接 `:` 或 `]`；不分大小寫 | `[WARN] …`、`Warning: …`、`error: …`、`[CRITICAL] …` |
| B. 前 80 字內的全大寫關鍵字 | 前 80 個字元內，**獨立、全大寫**的關鍵字 | `2026-10-08 12:00:00 ERROR [main] …`、`ERROR - …`、`… SEVERE [main] …` |

- 找到的話設 `log.level`，並標 `log.level_inferred: true`。應用自己寫了 level 的不會被覆蓋。
- **刻意不算的：**
  - 小寫散在句中：`0 errors`、`no error found`。
  - 首字母大寫但不在行首：`Errors: 0` 的 `Errors` 不是 level 關鍵字。
  - 連在別的字上：`ERROR_CODE=5`。
  - 在第 80 個字元之後。
- **仍可能誤判**，例如訊息開頭剛好是 `NO ERROR`。所以 AIOps 對 `log.level_inferred: true` 的資料要降低權重。

### level 正規化

不管是應用自己寫的，還是猜出來的，一律統一成六種：

| 原本的寫法（不分大小寫） | 統一成 |
|---|---|
| `trace` | trace |
| `debug` | debug |
| `info`、`notice` | info |
| `warn`、`warning` | warn |
| `error`、`err`、`severe`（Java 內建 logging、Tomcat） | error |
| `fatal`、`critical`、`crit`、`panic`、`emerg`、`alert` | fatal |

不認得的值，例如 `verbose`，轉小寫後原樣保留，不硬塞進六種。

案例測試見 [test/pipeline/level-cases.json](../test/pipeline/level-cases.json)，不用 K8s，幾秒跑完：

```bash
ES_URL=https://<es>:9200 ES_AUTH=elastic:<password> test/pipeline/run-level-cases.sh
```

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
4. 跑 `elasticsearch-setup.sh`（`SKIP_KEYS=1`）裝到中央。**各 cluster 的 Collector 不用動。**
5. 服務在 values 填上 `logFormat`。

可以**先讓服務宣告格式、之後再補規則**：還沒有規則的期間，log 照收、原文完整，只是沒拆欄位。規則裝上去之後，新進來的資料就會拆。

**例外：一則訊息跨很多行的格式**（例如 SECS 的 SML body）。多行要在收集器收的時候就合併成一筆，送到中央就拼不回來了，所以這種格式除了第 1 步，還要在 Collector 的 `file_log` 加多行合併的規則（`recombine`）。

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
kubectl create namespace otel-collector
kubectl -n otel-collector create secret generic es-writer-keys \
  --from-literal=IT_ES_API_KEY=<encoded> \
  --from-literal=IMC_ES_API_KEY=<encoded> \
  --from-literal=UNASSIGNED_ES_API_KEY=<encoded>
```

log 和 metrics 用同一個 Collector、同一份 Secret。

在 Rancher 上建或改這個 Secret：

1. 切到目標 cluster，進 Storage 底下的 Secrets（路徑依 Rancher 版本略有不同）。
2. 選 `otel-collector` namespace，**不是應用的 namespace**。
3. 型別 Opaque，用 key/value 的編輯模式逐一加 `IT_ES_API_KEY`、`IMC_ES_API_KEY`、`UNASSIGNED_ES_API_KEY`。
4. 存檔後**不用重啟**：Secret 以檔案掛進 Collector，K8s 約 1 分鐘內更新檔案，Collector 偵測到就換用新 key（log 會印 `refresh token`）。

改完用 `kubectl -n otel-collector describe secret es-writer-keys` 看每個 key 的 byte 數：**API key 的 encoded 是 60 個字元，多 1 就是結尾多了換行。**

改 Secret 的坑：

- **貼 key 不要多出換行或空白。** 最常見的失敗，多一個看不見的換行就一直 401，錯誤訊息不會告訴你原因。**401 期間的資料會被 Collector 丟掉**（見「坑」）。
- **不要用「Edit as YAML」貼部分內容。** 那等於整份取代，沒貼到的 key 會消失。用 key/value 編輯模式。
- **namespace 和 cluster 很容易選錯**，Rancher UI 上尤其。
- **UI 改沒有紀錄可追**，誰在什麼時候改了什麼只能翻 audit log。
- **擴廠時這樣不可行**：每開一個新廠都要手動進 UI 建一次。見 README 待定事項。

改好 `platform/cluster-addons/otel-collector/kustomization.yaml` 裡的 `CLUSTER_NAME`、`ES_ENDPOINT` 和 image 路徑，然後：

```bash
kubectl apply -k platform/cluster-addons/otel-collector
```

**從 Fluent Bit 換過來時：** Collector 第一次啟動時，已經存在的 log 檔從尾巴開始讀，所以先部署 Collector、確認 `agent.type: otelcol` 的資料進來了，再移除 Fluent Bit。兩個並行的期間每行會有兩筆（`agent.type` 不同）。

### 換 key

用腳本換，不掉資料、不重啟 Collector：

```bash
ES_URL=https://<es>:9200 ES_AUTH=elastic:<password> platform/central/rotate-writer-key.sh imc <cluster 的 kube context>...
```

它照「先發新的 → 換進每個 cluster 的 Secret → 確認每個 Collector 都換用新 key → 才作廢舊的」做，新舊 key 有一段時間同時有效，所以沒有空檔。**這把 key 在哪些 cluster 用，就要全部列上**；任何一個 Collector 5 分鐘內沒換到，腳本會停下來、不作廢舊 key。

實測（[run-key-rotation.sh](../test/local-k3s/run-key-rotation.sh)）：IMC 每秒 200 行 log、metrics 每 30 秒一筆的情況下換 IMC 的 key，Secret 改了之後約 70 秒兩個 Collector 都換好；**log 59,714 筆 0 遺失 0 重複、metrics 沒有空檔、Collector 沒有重啟、沒有出現 401**，舊 key 已作廢。

**不要直接在 ES 作廢 key 再換 Secret**：中間那段 key 無效，Collector 收到 401 會把資料丟掉。

### 3. namespace 標上 dept

```bash
kubectl label namespace <ns> dept=D3120
```

Collector 會即時看到 namespace label 的變化，**標了之後幾秒內就生效**（metrics 實測 2～17 秒）；標之前的資料在 unassigned。

## 驗證

```bash
test/local-k3s/run-e2e.sh
```

在單節點 k3s 上跑過（ES 9.5.5、OpenTelemetry Collector contrib 0.162.0），**54 項全部通過**；另有 23 個 level 萃取案例全部通過。

- **分流**：三個 namespace 各自只進自己的 index，沒有重複送。
- **欄位**：logback 格式、ECS 巢狀格式、ECS 點號格式（ES 自己的 log）、純文字，四種都正確轉換；`agent.type` 是 `otelcol`。
- **依格式分派**：宣告 `logfmt` 的服務走 logfmt 規則；宣告了還沒有規則的 `secs`，退回 default，原文完整保留。
- **保底抓 level**：`[WARN] …`、`ERROR - …`、`Warning: …`、時間戳後的 `ERROR` 都抓得到；`0 errors`、`no error found`、`ERROR_CODE` 不會被誤抓；應用自己寫了 level 的不會被覆蓋，`WARNING`、`Err` 這類寫法會統一。
- **權限**：寫入 key 只能寫自己的 group，不能讀；AIOps key 能讀全部 k8s-*，不能寫。

可靠度與量（[load/run-load.sh](../test/local-k3s/load/run-load.sh)，產生器每行帶序號，用序號數遺失與重複）：

| 情境 | 遺失 | 重複 | 其他 |
|---|---|---|---|
| 每秒 2,000 行 × 120 秒 | 0 | 0 | 延遲 p50 0.6s／p99 1.0s；節點 Collector CPU 10%、記憶體最高 300MB（含 metrics） |
| 每秒 5,000 行 × 120 秒 | 0 | 0 | 延遲 p50 0.3s／p99 0.6s；CPU 22%、記憶體最高 312MB |
| 重啟 Collector 兩次（每秒 200 行） | **0** | 0 | Fluent Bit 時每次重啟掉幾筆到 2 秒 |
| ES 正常關機 3 分鐘 | 0 | 0 | ES 恢復後 23 秒補完 |
| ES `kill -9` 兩次（第 2 次在補送中） | 0 | 0 | 重送的由 `_id` 擋掉 |
| key 作廢 3 分鐘 | **失效那段全部遺失** | 0 | 見「坑」，換 key 要照順序 |

記憶體：節點 Collector 只收 metrics 時約 217MB，加上 log 後平常約 290MB（多約 75MB）；拿掉的 Fluent Bit 原本約 25～50MB。

Fluent Bit 時期的測資與結果（分流、欄位、大流量、ES 停機、去重複）見 [elasticsearch-testing.md](elasticsearch-testing.md)；中央 pipeline 跟當時是同一套，欄位的結論不變。

正式上線後，用 AIOps key 查一次，確認每個 namespace 只落在一個 group：

```
GET k8s-*/_search
{"size": 0, "aggs": {"ns": {"terms": {"field": "kubernetes.namespace", "size": 500},
  "aggs": {"group": {"terms": {"field": "labels.group"}}}}}}
```

## 坑

- **Collector 不能收自己的 log。** ES 拒收時它會印錯誤；如果再收回來送一次，就是放大迴圈（Fluent Bit 時期實測過：一次 key 設錯，十幾分鐘就灌進一萬七千多筆）。`node.yaml` 的 `file_log` 已經排除 `otel-collector` namespace，不要拿掉。
- **key 失效期間的 log 會遺失。** OTLP 出口只對 429、502、503、504 和連不上重試，401（key 錯、被作廢）當成永久錯誤直接丟掉，沒有設定可以改。換 key 用 `rotate-writer-key.sh`（見「[換 key](#換-key)」），不要手動先作廢再換。
- **ES 拒收其中幾筆時也會丟。** 例如欄位型別衝突（400）：Collector 只印 `Partial success ... dropped_log_records`。中央 pipeline 有錯誤處理，這種情況很少見，但要監控 Collector 的 log。`CONFLICT`（409）是重送的被 `_id` 擋掉，屬於正常。
- **中央 ES 改了 `k8s-logs-otel`、`logs-otel@custom` 之後，要讓 `logs-*.otel-*` 換新的 backing index 才生效。** `elasticsearch-setup.sh` 會自動檢查並 rollover。沒生效的話，log 會停在 `logs-generic.otel-*`，不會進 `k8s-*`。
- **改了 `_index` 之後，ES 不會自動跑目的地的 default pipeline。** 所以 `k8s-logs-otel` 最後自己呼叫 `k8s-logs`，不要拿掉。
- **ES 停機期間，資料堆在節點上的佇列**（`/var/lib/otelcol/node`），每個 group 上限 10,000 批，每批最多 2,000 行，所以量很大或停很久時要估算。
- **讀到檔案哪裡也記在 `/var/lib/otelcol/node`。** 這個目錄要在節點上（hostPath），Pod 重建才會接著讀；刪掉這個目錄，下次會從檔案尾巴開始讀，中間的 log 就沒了。
- **第一次部署時，已經存在的 log 檔從尾巴開始讀**（`start_at: end`），舊的 log 不會補收。
- **kubelet 預設 log 檔 10MB 就輪替壓縮。** 量大的服務在 Collector 讀完前檔案就被壓縮的話，那段會掉（Fluent Bit 時期實測過）。Collector 每 0.2 秒掃一次（`poll_interval`），每秒 5,000 行實測沒掉；平台端仍建議把 kubelet 的 `container-log-max-size` 調大，例如 50Mi。
- **平台元件的 log 都進 unassigned**，例如 kube-system 和其他系統元件。這跟 metrics 的兜底 tenant 同一個意思。
- **純文字的 Java stack trace 會被拆成很多行。** 標準格式是 JSON（stack trace 在一個欄位裡），所以不處理。沒照標準的服務，AIOps 在它身上的效果會比較差。
- **中央 ES 要撐得住掛一台，至少要三個 master 候選。** 兩個節點時 ES 只讓其中一個投票，那台掛了整個 cluster 就停擺，跟一個節點一樣（實測見 [elasticsearch-testing.md](elasticsearch-testing.md#情境測試es-兩個節點-vs-一個節點)）。三台一樣的節點，或兩台存資料加一台只投票的 `voting_only` 節點。ES 停擺期間資料會在 Collector 排隊，恢復後補齊，不會遺失。
- **ES 當機（kill -9）不會掉已經回成功的資料。** 實測砍兩次（一次在補送中），log 70,944 筆 0 遺失 0 重複，metrics 沒缺（見 [elasticsearch-testing.md](elasticsearch-testing.md#情境測試es-當機kill--9)）。前提是 `index.translog.durability` 保持預設的 `request`，不要改成 `async`。
- **一個節點時 metrics 平常是 yellow。** metrics 的資料流預設要一份副本，一個節點放不下；log 的設定是有幾個節點就放幾份（`auto_expand_replicas: 0-1`），所以 log 是 green。不影響寫入。
- **節點用 Docker 當 runtime 的 cluster 要另外掛 `/var/lib/docker/containers`。** RKE2、k3s 是 containerd，不需要。

## 還沒做

1. **K8s events**（OOMKilled、probe 失敗、BackOff）。這是 AIOps 很重要的訊號，但 event 本身只帶 namespace 名稱，不帶 dept；要分流得先決定歸屬放哪裡，見 README 待定事項第 1 點。唯讀角色裡已經預留了 `k8s-events-{group}-*`。
2. **保留天數**要各 group 確認。
3. **⚠️ 重點：SECS、REST API 等格式的解析規則**：架構已經好了（見「[pipeline](#pipeline)」），等拿到真實的 log 樣本再寫。SECS 還要在 Collector 的 `file_log` 加多行合併。
4. **dept 清單目前要手動同步兩處**：`groups.yaml`、Collector 的 `common.yaml`（log 與 metrics 共用）。group 變多之前要改成從 `groups.yaml` 產生。

# Log 進 Elasticsearch：操作

> **類型：操作 How-To**　同主題：[原理](README.md)・[規格（欄位、pipeline）](reference.md)・[Fluent Bit vs OTel（原理）](fluentbit-vs-otel.md)

怎麼部署、怎麼驗證、遇到問題怎麼辦。

## 部署

### 1. 中央 ES（一次）

```bash
ES_URL=https://<es>:9200 ES_AUTH=elastic:<password> telemetry/scripts/elasticsearch-setup.sh
```

輸出裡每把 key 的 `encoded` 要記下來。只更新 index 結構、不發新 key 時，加上 `SKIP_KEYS=1`。

### 2. 每個應用 cluster

```bash
kubectl create namespace otel-collector
kubectl -n otel-collector create secret generic es-writer-keys \
  --from-literal=ES_WRITER_API_KEY=<encoded>
```

log、metrics、trace 用同一份 Secret、同一把 key（setup 輸出的「寫入 key：es-writer」）（[pdca/phase1-collect/2026-10-ingest-simplify.md](../pdca/phase1-collect/2026-10-ingest-simplify.md)）。

在 Rancher 上建或改這個 Secret：

1. 切到目標 cluster，進 Storage 底下的 Secrets（路徑依 Rancher 版本略有不同）。
2. 選 `otel-collector` namespace，**不是應用的 namespace**。
3. 型別 Opaque，用 key/value 的編輯模式加 `ES_WRITER_API_KEY`。
4. 存檔後**不用重啟**：Secret 以檔案掛進 Collector，K8s 約 1 分鐘內更新檔案，Collector 偵測到就換用新 key（log 會印 `refresh token`）。

改完用 `kubectl -n otel-collector describe secret es-writer-keys` 看 key 的 byte 數：**API key 的 encoded 是 60 個字元，多 1 就是結尾多了換行。**

改 Secret 的坑：

- **貼 key 不要多出換行或空白。** 最常見的失敗，多一個看不見的換行就一直 401，錯誤訊息不會告訴你原因。**401 期間的資料會被 Collector 丟掉**（見「坑」）。
- **不要用「Edit as YAML」貼部分內容。** 那等於整份取代，沒貼到的 key 會消失。用 key/value 編輯模式。
- **namespace 和 cluster 很容易選錯**，Rancher UI 上尤其。
- **UI 改沒有紀錄可追**，誰在什麼時候改了什麼只能翻 audit log。
- **擴廠時這樣不可行**：每開一個新廠都要手動進 UI 建一次。見 README「還沒定案的」第 3 點。

改好 `telemetry/collectors/kustomization.yaml` 裡的 `CLUSTER_NAME`、`ES_ENDPOINT` 和 image 路徑，然後：

```bash
kubectl apply -k telemetry/collectors
```

**從 Fluent Bit 換過來時：** Collector 第一次啟動時，已經存在的 log 檔從尾巴開始讀，所以先部署 Collector、確認 `agent.type: otelcol` 的資料進來了，再移除 Fluent Bit。兩個並行的期間每行會有兩筆（`agent.type` 不同）。

### 換 key

用腳本換，不掉資料、不重啟 Collector：

```bash
ES_URL=https://<es>:9200 ES_AUTH=elastic:<password> telemetry/scripts/rotate-writer-key.sh imc <cluster 的 kube context>...
```

它照「先發新的 → 換進每個 cluster 的 Secret → 確認每個 Collector 都換用新 key → 才作廢舊的」做，新舊 key 有一段時間同時有效，所以沒有空檔。**這把 key 在哪些 cluster 用，就要全部列上**；任何一個 Collector 5 分鐘內沒換到，腳本會停下來、不作廢舊 key。

實測（[run-key-rotation.sh](../../e2e/local-k3s/run-key-rotation.sh)）：IMC 每秒 200 行 log、metrics 每 30 秒一筆的情況下換 IMC 的 key，Secret 改了之後約 70 秒兩個 Collector 都換好；**log 59,714 筆 0 遺失 0 重複、metrics 沒有空檔、Collector 沒有重啟、沒有出現 401**，舊 key 已作廢。

**不要直接在 ES 作廢 key 再換 Secret**：中間那段 key 無效，Collector 收到 401 會把資料丟掉。

### 3. namespace 標上 dept

```bash
kubectl label namespace <ns> dept=D3120
```

Collector 會即時看到 namespace label 的變化，**標了之後幾秒內就生效**（metrics 實測 2～17 秒）；標之前的資料在 unassigned。

## 驗證

```bash
e2e/local-k3s/run-e2e.sh
```

在 k3d 單節點上跑過（ES 8.19.23、OpenTelemetry Collector contrib 0.162.0），**54 項全部通過**；另有 24 個 level 萃取案例全部通過。

- **分流**：三個 namespace 各自只進自己的 index，沒有重複送。
- **欄位**：logback 格式、ECS 巢狀格式、ECS 點號格式（ES 自己的 log）、純文字，四種都正確轉換；`agent.type` 是 `otelcol`。
- **依格式分派**：宣告 `logfmt` 的服務走 logfmt 規則；宣告了還沒有規則的 `secs`，退回 default，原文完整保留。
- **保底抓 level**：`[WARN] …`、`ERROR - …`、`Warning: …`、時間戳後的 `ERROR` 都抓得到；`0 errors`、`no error found`、`ERROR_CODE` 不會被誤抓；應用自己寫了 level 的不會被覆蓋，`WARNING`、`Err` 這類寫法會統一。
- **權限**：寫入 key 一把、所有 group 共用，只能寫、不能讀（資料屬於哪個 group 由 Collector 依 Pod 決定）；AIOps key 能讀全部 k8s-*，不能寫；人用各 group 的唯讀角色。
- **被拒的文件**：pipeline 失敗、型別不合的留在 failure store（`k8s-<group>-<cluster>::failures`），`quality.ingest_rejected` 有一筆就報（[pdca/phase1-collect/2026-10-ingest-simplify.md](../pdca/phase1-collect/2026-10-ingest-simplify.md)）。

可靠度與量（[load/run-load.sh](../../e2e/local-k3s/load/run-load.sh)，產生器每行帶序號，用序號數遺失與重複）：

| 情境 | 遺失 | 重複 | 其他 |
|---|---|---|---|
| 每秒 2,000 行 × 120 秒 | 0 | 0 | 延遲 p50 0.6s／p99 1.0s；節點 Collector CPU 10%、記憶體最高 300MB（含 metrics） |
| 每秒 5,000 行 × 120 秒 | 0 | 0 | 延遲 p50 0.3s／p99 0.6s；CPU 22%、記憶體最高 312MB |
| 重啟 Collector 兩次（每秒 200 行） | **0** | 0 | Fluent Bit 時每次重啟掉幾筆到 2 秒 |
| ES 正常關機 3 分鐘 | 0 | 0 | ES 恢復後 23 秒補完 |
| ES `kill -9` 兩次（第 2 次在補送中） | 0 | 0 | 重送的由 `_id` 擋掉 |
| key 作廢 3 分鐘 | **失效那段全部遺失** | 0 | 見「坑」，換 key 要照順序 |

更大量與邊緣情況（[load/run-stress.sh](../../e2e/local-k3s/load/run-stress.sh)，[pdca/phase1-collect/2026-10-load-test.md](../pdca/phase1-collect/2026-10-load-test.md)）：每秒 10,000 行掉 0、延遲 p95 0.3 秒；20,000 行掉 0 但晚到約 2 分鐘；64 KB、1 MB 的單行不切、不截斷。**寫入 index 大過 ES 記憶體會停住**（指紋 `_id` 去重每筆要查 `_id`），ILM 要依大小 rollover。

記憶體：節點 Collector 只收 metrics 時約 217MB，加上 log 後平常約 290MB（多約 75MB）；拿掉的 Fluent Bit 原本約 25～50MB。

中央 pipeline 跟 Fluent Bit 時期是同一套，分流、欄位、去重複的結論不變。

正式上線後，用 AIOps key 查一次，確認每個 namespace 只落在一個 group：

```
GET k8s-*/_search
{"size": 0, "aggs": {"ns": {"terms": {"field": "kubernetes.namespace", "size": 500},
  "aggs": {"group": {"terms": {"field": "labels.group"}}}}}}
```

## 坑

集中在 [troubleshooting.md](../troubleshooting.md#log)。

## 服務怎麼宣告格式

用 app-common chart 的服務，在 values 填 `logFormat`，chart 會在 pod 加上 label `log-format`：

```yaml
logFormat: logfmt
```

- 名稱只能是小寫英數與減號，填錯部署直接失敗。
- 照 Java 標準輸出的服務不用填。
- 沒用 chart 的服務，直接在 pod template 加 label `log-format: <格式>`。

## 新增一種格式（例如 SECS）

1. 在 [formats/](../../telemetry/storage/formats) 加 `<格式>.json`。只要做到「原文 → `_app`」，再交給 `k8s-logs-fields`。可以參考 [logfmt.json](../../telemetry/storage/formats/logfmt.json)。需要寫程式的話，放同名的 `<格式>.painless`。
2. 規則裡第一步一定要 `set log.parser = <格式>`，解析失敗時不能丟掉原文（`ignore_failure` 或自己的 `on_failure`）。
3. 在 [e2e/local-k3s/](../../e2e/local-k3s) 加一個輸出該格式的測試服務和檢查項目，跑 `run-e2e.sh`。
4. 跑 `elasticsearch-setup.sh`（`SKIP_KEYS=1`）裝到中央。**各 cluster 的 Collector 不用動。**
5. 服務在 values 填上 `logFormat`。

可以**先讓服務宣告格式、之後再補規則**：還沒有規則的期間，log 照收、原文完整，只是沒拆欄位。規則裝上去之後，新進來的資料就會拆。

**例外：一則訊息跨很多行的格式**（例如 SECS 的 SML body）。多行要在收集器收的時候就合併成一筆，送到中央就拼不回來了，所以這種格式除了第 1 步，還要在 Collector 的 `file_log` 加多行合併的規則（`recombine`）。

## 查哪些格式還沒有規則

```
GET k8s-*/_search
{"size": 0, "query": {"bool": {"must_not": {"term": {"log.format": "default"}}}},
 "aggs": {"format": {"terms": {"field": "log.format"}, "aggs": {"parser": {"terms": {"field": "log.parser"}}}}}}
```

`log.format` 是 `secs`、`log.parser` 卻是 `default` 的，就是宣告了但還沒寫規則。
